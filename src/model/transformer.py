from src.model.decoder import *
from src.model.encoder import *
from src.model.noise_model import *
from src.data.load_embeddings import *
from src.utils.config import params
from torch.distributions.kl import kl_divergence
import logging

class Transformer(torch.nn.Module):

    def __init__(self, data_params, logger, is_variational,
                 embd_file=None, init_emb=True,
                 is_shared_emb=True,
                 use_word_drop=True):
        """
        :param n_langs: number of supported languages
        :param is_shared_emb: languages use shared embeddings
        """
        super(Transformer, self).__init__()
        assert (type(is_shared_emb) is bool)

        self.logger = logger
        self.d_model = params["d_model"]
        self.n_layers = params["n_layers"]
        self.dff = params["dff"]
        self.d_k = params["d_k"]

        self.is_shared_emb = is_shared_emb
        self.is_variational = is_variational
        self.use_word_drop = use_word_drop
        self.word_drop = params["word_drop"]

        # will be set in load_data
        self.data = None
        self.bos_index = None
        self.eos_index = None
        self.pad_index = None
        self.blank_index = None
        self.noise_model = None
        self.languages = None
        self.dictionaries = None
        self.vocab_size = None

        if data_params is not None:
            self.load_data(data_params)
            self.n_langs = len(self.languages)

        # for debugging
        else:
            self.n_langs=2
            self.vocab_size=[500, 500]
            self.id2lang = None
            self.bos_index = [0, 5]
            self.eos_index = 1
            self.pad_index = 2
            self.blank_index = 4

        self.encoder = StackedEncoder(n_layers=self.n_layers,
                                      params=params,
                                      n_langs=self.n_langs,
                                      vocab_size=self.vocab_size,
                                      is_shared_emb=is_shared_emb,
                                      freeze_emb=init_emb)

        self.decoder = StackedDecoder(n_layers=self.n_layers,
                                      params=params,
                                      n_langs=self.n_langs,
                                      vocab_size=self.vocab_size,
                                      is_shared_emb=is_shared_emb,
                                      freeze_emb=init_emb)

        linear = torch.nn.Linear(self.d_model, self.vocab_size[0])

        if self.is_shared_emb:
            self.linear_layers = torch.nn.ModuleList([linear for _ in range(self.n_langs)])

        else:
            self.linear_layers = torch.nn.ModuleList([torch.nn.Linear(self.d_model, self.vocab_size[l]) for l in range(self.n_langs)])

        if is_variational:
            # compute sigma, assume diagonal for now, shape = batch_size, len, d_model
            self.compute_sigma = torch.nn.Sequential(torch.nn.Linear(self.d_model, self.d_model),
                                                     torch.nn.Tanh(),
                                                     torch.nn.Linear(self.d_model, self.d_model),
                                                     torch.nn.Softplus())

        def init_weights(m):

            if type(m) == torch.nn.Linear:
                torch.nn.init.xavier_uniform(m.weight)

                if m.bias is not None:
                    torch.nn.init.constant(m.bias, 0)

        self.encoder.apply(init_weights)
        self.decoder.apply(init_weights)

        if init_emb and embd_file is not None:
            self.initialize_embeddings(embedding_file=embd_file)

        else:
            for l in self.linear_layers:
                l.apply(init_weights)

    def encode(self, input_seq, src_mask, src_lang, n_samples=1, return_kl=True):

        z = self.encoder(input_seq, src_mask=src_mask, lang_id=src_lang)

        if not self.is_variational or n_samples <= 0:
            return z

        else:
            new_z, kl_div = self.sample_z(z, n_samples)

            if return_kl:
                return new_z, kl_div
            else:
                return new_z

    def decode(self, prev_output, latent_seq, src_mask, tgt_mask, tgt_lang):

        dec_output = self.decoder(prev_output,
                                  latent_seq,
                                  src_mask=src_mask,
                                  tgt_mask=tgt_mask,
                                  lang_id=tgt_lang)

        return self.linear_layers[tgt_lang](dec_output)

    def get_emb(self, input_seq, src_mask, src_lang):

        z = self.encoder(input_seq, src_mask=src_mask, lang_id=src_lang)

        # compute mean along dim of len which is 1,
        # note that this will keep track of the gradient
        sent_emb = torch.mean(z, dim=1)
        return sent_emb

    def sample_z(self, z, n_samples):
        """
        computes sentence embedding using the average of the sentences,
        samples sentence embedding, then shifts the other z's to have that new average
        :param z: determinisic output of encoder shape [n_samples, batch_size, len, d_model]
        :param n_samples:
        :return: latent variables of shape [n_samples, batch_size, len, d_model]
        """
        # compute mean along dim of len which is 1, note that this will keep track of the gradient
        sent_emb = torch.mean(z, dim=1)

        # compute diagonal elements of sigma, returns vectors of dim d_model
        sigma = self.compute_sigma(sent_emb)

        # make sigma a diagonal matrix of shape batch size, dim, dim
        sigma = sigma.unsqueeze(-1).expand(*sigma.size(), self.d_model)
        sigma = sigma * torch.eye(self.d_model, device=sigma.device)

        shift_dist = torch.distributions.MultivariateNormal(loc=torch.zeros_like(sent_emb),
                                                            covariance_matrix=sigma)

        posterior = torch.distributions.MultivariateNormal(loc=sent_emb,
                                                           covariance_matrix=sigma)

        prior = torch.distributions.MultivariateNormal(loc=torch.zeros(self.d_model, device=sent_emb.device),
                                                       covariance_matrix=torch.eye(self.d_model, device=sent_emb.device))

        # samples z using reparameterization trick, the gradient will be propagated back
        shift = shift_dist.rsample(sample_shape=torch.Size([n_samples]))

        # repeat shift on the len dim, n_samples, batch_size, ?, d_model
        shift = shift.unsqueeze_(2).repeat(1, 1, z.size(1), 1)

        # repeat z on the sample dim, allocates more memory
        z = z.unsqueeze_(0).repeat(n_samples, 1, 1, 1)

        # shift all the z's by the new avg
        z = z + shift
        z = z.view(n_samples*z.size(1), -1, self.d_model)
        kl_div = torch.mean(kl_divergence(prior, posterior))

        return z, kl_div

    def forward(self, input_seq, prev_output, src_mask, tgt_mask, src_lang, tgt_lang):

        if self.is_variational:
            latent, kl_div = self.encode(input_seq, src_mask, src_lang)
            prev_output = self.word_dropout(prev_output=prev_output, lang_id=tgt_lang)

        else:
            latent = self.encode(input_seq, src_mask, src_lang)

        dec_outputs = self.decode(prev_output=prev_output,
                                  latent_seq=latent,
                                  src_mask=src_mask,
                                  tgt_mask=tgt_mask,
                                  tgt_lang=tgt_lang)

        if self.is_variational:
            # return all the things!
            return dec_outputs, kl_div, latent

        else:
            return dec_outputs

    def word_dropout(self, prev_output, lang_id):

        if self.word_drop == 0:
            return prev_output

        assert 0 < self.word_drop < 1

        # define words to blank
        bos_index = self.bos_index[lang_id]
        keep = torch.rand(prev_output.size(0), prev_output.size(1)) >= self.word_drop
        keep = keep.type(torch.LongTensor)
        keep = keep.to(prev_output.device)
        keep[:, 0] = 1  # do not blank the start sentence symbol

        prev_output_new = torch.ones_like(prev_output)*self.blank_index
        prev_output_new = prev_output*keep + prev_output_new*(1-keep)

        return prev_output_new

    def load_data(self, data_params):

        all_data = load_data(data_params)
        self.data = all_data
        self.data_params = data_params
        print(all_data)

        self.languages = list(all_data['dico'].keys())
        self.id2lang = {i: lang for i, lang in enumerate(self.languages)}

        self.dictionaries = all_data['dico']
        self.vocab_size = [len(self.dictionaries[l].word2id) for l in self.languages]

        # by construction, special indices are the same for all languages
        self.pad_index = data_params.pad_index
        self.eos_index = data_params.eos_index
        self.bos_index = data_params.bos_index
        self.blank_index = data_params.blank_index

        print("pad_index", self.pad_index)
        print("eos_index", self.eos_index)
        print("bos_index", self.bos_index)
        print("unk_index", data_params.unk_index)
        print("blank_index", data_params.blank_index)


    def initialize_embeddings(self, embedding_file):

        if embedding_file == '':
            return

        split = embedding_file.split(',')

        # for shared embeddings
        if len(split) == 1:
            assert os.path.isfile(embedding_file)
            pretrained_0, word2id_0 = reload_embeddings(embedding_file, self.d_model)

            # replicate shared embeddings for each language
            pretrained = [pretrained_0 for _ in range(self.n_langs)]

            # replicate shared dictionary for each language
            word2id = [word2id_0 for _ in range(self.n_langs)]

        else:
            assert len(split) == self.n_langs
            assert not self.is_shared_emb
            assert all(os.path.isfile(x) for x in split)
            pretrained = []
            word2id = []
            for path in split:
                pretrained_i, word2id_i = reload_embeddings(path, self.emb_dim)
                pretrained.append(pretrained_i)
                word2id.append(word2id_i)

        found = [0 for _ in range(self.n_langs)]
        lower = [0 for _ in range(self.n_langs)]

        # for every language
        for i, lang in enumerate(self.languages):

            # if shared embeddings across languages, just do this once
            if self.is_shared_emb and i > 0:
                break

            # define dictionary / parameters to update
            dico = self.data['dico'][lang]

            # update the embedding layer of the encoder & decoder, for language i
            to_update = [self.encoder.embedding_layers[i].weight.data]
            to_update.append(self.decoder.embedding_layers[i].weight.data)
            to_update.append(self.linear_layers[i].weight.data)

            # for every word in that language
            for word_id in range(self.vocab_size[i]):
                word = dico[word_id]

                # if word is in the dictionary of that language
                if word in word2id[i]:

                    # count the number of words found for each language
                    found[i] += 1

                    # get the embedding vector for that word
                    vec = torch.from_numpy(pretrained[i][word2id[i][word]])

                    # for each embedding layer to update
                    # set the word_id's word vector to vec
                    for x in to_update:
                        x[word_id] = vec

                # if word requires lowercasing
                elif word.lower() in word2id[i]:
                    found[i] += 1
                    lower[i] += 1
                    vec = torch.from_numpy(pretrained[i][word2id[i][word.lower()]])
                    for x in to_update:
                        x[word_id] = vec

        # print summary
        for i in range(self.n_langs):
            _found = found[0 if self.is_shared_emb  else i]
            _lower = lower[0 if self.is_shared_emb  else i]
            self.logger.info(
                "Initialized %i / %i word embeddings for \"%s\" (including %i "
                "after lowercasing)." % (_found, self.vocab_size[i], i, _lower)
            )

if __name__ == "__main__":
    # test transformer

    x = torch.zeros(20, 5, dtype=torch.int64)
    y = torch.zeros(20, 7, dtype=torch.int64)
    tgt_m = np.tril(np.ones((1, 7, 7)), k=0).astype(np.uint8)
    tgt_m = torch.from_numpy(tgt_m)

    src_m = torch.zeros(20, 5).unsqueeze(-2).unsqueeze(-2)

    # test lm_loss
    # x = torch.ones(2, 5, dtype=torch.int64)
    # x[:, -2:] = model.pad_index
    # loss = model.lm_loss(x, 1)
    # print(loss)

    # Test word drop
    # parser = get_parser()
    # data_params = parser.parse_args()
    # check_all_data_params(data_params)
    model = Transformer(data_params=None, embd_file=None, logger=logging)

    out = model.forward(input_seq=x, prev_output=y, src_lang=0, tgt_lang=1, src_mask=src_m, tgt_mask=tgt_m)
    print(out)

