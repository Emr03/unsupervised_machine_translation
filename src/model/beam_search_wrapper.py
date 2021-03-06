from src.model.transformer import *
from src.onmt.translate.beam import *
from src.onmt.translate.beam_search import BeamSearch
from logging import getLogger
from src.utils.beam_search_utils import tile
import torch.nn as nn

class MyBeamSearch(torch.nn.Module):
    '''
    Wrapper around OpenNMT beam search that suits our purposes
        dico: the dictionary of vocabulary
        beam_size: beam size parameter for beam search
        batch_size: batch size
        n_best: don't stop until we reached n_best sentences (sentences that hit EOS)
        mb_device: the type of device. See https://pytorch.org/docs/stable/tensor_attributes.html#torch.torch.device
        encoding_lengths: LongTensor of encoding lengths
        max_length: Longest acceptable sequence, not counting begin-of-sentence (presumably there has been no EOS yet if max_length is used as a cutoff)
    '''
    def __init__(self, transformer, beam_size, n_best,
                 encoding_lengths, max_length, logger):

        super(MyBeamSearch, self).__init__()
        self.beam_size = beam_size
        self.max_length = max_length
        self.logger = logger

        self.pad_index = transformer.module.pad_index
        self.eos_index = transformer.module.eos_index
        self.bos_index = transformer.module.bos_index
        self.id2lang = transformer.module.id2lang
        self.transformer = transformer.module.eval()

        self.n_best = n_best
        self.encoding_lengths = encoding_lengths
        self.max_length = max_length

    '''
    Performs beam search on a batch of sequences
    Adapted from _translate_batch in translator.py from onmt
    Returns: hypotheses (list[list[Tuple[Tensor]]]): Contains a tuple
            of score (float), sequence (long), and attention (float or None).
    '''
    def forward(self, batch, src_mask, src_lang, tgt_lang, random=False):

        batch_size = batch.size(0)

        # get device
        if batch.is_cuda:
            device = batch.get_device()

        else:
            device = torch.device('cpu')

        # if parallel, each BeamSearch object lives on the device of the input batch
        beamSearch = BeamSearch(self.beam_size, batch_size,
                                     pad=self.pad_index,
                                     bos=self.bos_index[tgt_lang],
                                     eos=self.eos_index,
                                     n_best=self.n_best, mb_device=device,
                                     global_scorer=GNMTGlobalScorer(0.7, 0., "avg", "none"),
                                     min_length=0, max_length=self.max_length, return_attention=False,
                                     block_ngram_repeat=0,
                                     exclusion_tokens=set(),
                                     memory_lengths=self.encoding_lengths,
                                     stepwise_penalty=False, ratio=0.)

        # disable gradient tracking
        with torch.set_grad_enabled(False):

            # (1) Run the encoder on the src.
            enc_out = self.transformer.encode(batch,
                                          src_mask=src_mask,
                                          src_lang=src_lang,
                                          n_samples=1,
                                          return_kl=False)

            #self.logger.info("enc_out batch size %i " % (enc_out.size(0)))

            # (2) Repeat src objects `beam_size` times. along dim 0
            # We use batch_size x beam_size
            enc_out = enc_out.repeat(self.beam_size, 1, 1)
            src_mask = src_mask.repeat(self.beam_size, 1, 1, 1)
            #print("enc out", enc_out[:, :, 0])

            # dec_output should be batch_size x beam_size, dec_seq_len
            # in this first case it should be batch_size x 1 x hidden_size since it's just the first word generated
            dec_out = torch.ones(batch_size*self.beam_size, 1,
                                 dtype=torch.int64,
                                 device=batch.device)*self.bos_index[tgt_lang]

            for step in range(self.max_length):

                # decoder_input = self.beamSearch.current_predictions.view(-1, 1)
                # print("decoder_input", decoder_input.shape)

                # in case of inference tgt_len = 1, batch = beam times batch_size
                log_probs = self.transformer.decode(dec_out, enc_out, src_mask,
                                               tgt_mask=None, tgt_lang=tgt_lang)[:, -1, :]

                log_probs = F.log_softmax(log_probs, dim=-1)
                #print("log probs", log_probs.shape)

                #advance takes input of size batch_size*beam_size x vocab_size
                beamSearch.advance(log_probs, None)

                # check if any beam is finished (last output selected was eos)
                # note that this removes this node from select_indices
                # also adds the sentence to list of hypetheses, so you don't need to deal with it anymore
                any_beam_is_finished = beamSearch.is_finished.any()
                if any_beam_is_finished:
                    beamSearch.update_finished()
                    if beamSearch.done:
                        break

                # get chosen words by beam search
                next_word = beamSearch.current_predictions.unsqueeze_(-1)
                #next_word = self.beamSearch.current_predictions.view(self.batch_size*self.beam_size, -1)

                # get indices of expanded nodes, for each input sentence
                select_indices = beamSearch.current_origin
                #print("select_indices", select_indices)

                # select previous output of expanded nodes
                #self.logger.info("dec_out batch size %i" % (dec_out.size(0)))
                dec_out = dec_out[select_indices]
                enc_out = enc_out[select_indices]
                src_mask = src_mask[select_indices]
                # self.logger.info("select_indices %s" %(','.join(map(str, select_indices.data))))
                # self.logger.info("dec_out batch size %i" % (dec_out.size(0)))
                #print("dec_out", dec_out)

                #dec out should be batch_size x (previous_sentence_len + 1) x hidden_size
                dec_out = torch.cat((dec_out, next_word), 1)
                #print("current predictions" + str(self.beamSearch.current_predictions))
                #print("dec out", dec_out)

        # (batch_size) list of (beam_size) lists of tuples
        hypotheses = beamSearch.hypotheses
        sentences, len = self.format_sentences(hypotheses=hypotheses,
                                               tgt_lang=tgt_lang,
                                               device=batch.device)
        return sentences, len

    def format_sentences(self, hypotheses, device, tgt_lang, random=False):
        """

        :param hypotheses: list of lists
        :param random:
        :return:
        """

        # get lengths of sentences
        if random:
            indices = np.random.randint(low=0, high=self.beam_size, size=self.batch_size)
            sentences = list(map(lambda beams: beams[indices[i]][1]) for i, beams in enumerate(hypotheses))
            lengths = torch.cuda.LongTensor([s[indices[i]].shape[0] + 2 for i, s in enumerate(hypotheses)], device=device)

        else:
            sentences = list(map(lambda beams: beams[-1][1], hypotheses))
            lengths = torch.cuda.LongTensor([s.shape[0] + 2 for s in sentences], device=device)

        # fill unused sentence spaces with pad token
        sent = torch.cuda.LongTensor(lengths.size(0), lengths.max(), device=device).fill_(self.pad_index)

        # copy sentence tokens, don't overwrite bos, add eos
        for i, s in enumerate(sentences):
            sent[i, 0] = self.bos_index[tgt_lang]
            sent[i, 1:lengths[i] - 1].copy_(s)
            sent[i, lengths[i] - 1] = self.eos_index

        return sent, lengths


if __name__ == "__main__":

    from src.utils.config import params
    #batch_size x seq_len
    x = torch.zeros(2, 5, dtype=torch.int64)
    x[1, :] = torch.ones(5, dtype=torch.int64)

    src_m = torch.ones(2, 5)
    src_m[:, -2:-1] = 0
    src_m = src_m.unsqueeze(-2).unsqueeze(-2)

    # parser = get_parser()
    # data_params = parser.parse_args()
    # check_all_data_params(data_params)
    transformer = Transformer(data_params=None, logger=getLogger(), embd_file=None).eval()

    beam = MyBeamSearch(transformer, beam_size=3, n_best=2,
                        encoding_lengths=512, max_length=40, logger=None)

    sent, len = beam(x, src_m, src_lang=1, tgt_lang=1)
    print(sent, len)
