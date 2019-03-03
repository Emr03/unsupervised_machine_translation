from .config import params

from .encoder import *
from .decoder import *
from .sublayers import *
import numpy as np
from .data.loader import *
from .data_loading import get_parser
from .data.dataset import *
from .data.dictionary import PAD_WORD, EOS_WORD, BOS_WORD
from .pretrain_embeddings import *

class Transformer(torch.nn.Module):

    def __init__(self, n_langs, is_shared_emb=True):
        """

        :param n_langs: number of supported languages
        :param is_shared_emb: languages use shared embeddings
        """

        super(Transformer, self).__init__()
        self.d_model = params["d_model"]
        self.vocab_size = params["vocab_size"]
        self.n_layers = params["n_layers"]
        self.dff = params["dff"]
        self.d_k = params["d_k"]
        self.n_langs = n_langs
        self.is_shared_emb = is_shared_emb
        assert(type(is_shared_emb) is bool)

        self.encoder = StackedEncoder(n_layers=self.n_layers,
                                      params=params,
                                      n_langs=n_langs,
                                      is_shared_emb=is_shared_emb)

        self.decoder = StackedDecoder(n_layers=self.n_layers,
                                      params=params,
                                      n_langs=n_langs,
                                      is_shared_emb=is_shared_emb)

        linear = torch.nn.Linear(self.d_model, self.vocab_size) 
        
        if self.is_shared_emb:
            self.linear_layers = [linear for _ in range(self.n_langs)]
        
        else:
            self.linear_layers = [torch.nn.Linear(self.d_model, self.vocab_size) for _ in range(self.n_langs)]

        self.data = None

        def init_weights(m):

            if type(m) == torch.nn.Linear:
                torch.nn.init.xavier_uniform(m.weight)

                if m.bias is not None:
                    torch.nn.init.constant(m.bias, 0)

        self.encoder.apply(init_weights)
        self.decoder.apply(init_weights)
        #self.linear.apply(init_weights)

    def encode(self, input_seq, src_mask, src_lang):

        return self.encoder(input_seq, src_mask=src_mask, lang_id=src_lang)

    def decode(self, prev_output, latent_seq, src_mask, tgt_mask, tgt_lang):

        return self.decoder(prev_output, latent_seq, src_mask=src_mask, tgt_mask=tgt_mask, lang_id=tgt_lang)

    def forward(self, input_seq, prev_output, src_mask, tgt_mask, src_lang, tgt_lang):

        latent = self.encode(input_seq, src_mask, src_lang)

        dec_outputs = self.decode(prev_output=prev_output,
                                  latent_seq=latent,
                                  src_mask=src_mask,
                                  tgt_mask=tgt_mask,
                                  tgt_lang=tgt_lang)

        return F.softmax(self.linear_layers[tgt_lang](dec_outputs), dim=-1)

    def load_data(self, data_params):

        all_data = load_data(data_params)
        self.data = all_data
        print(all_data)

        self.languages = list(all_data['dico'].keys())

        self.mono_data_train = [all_data['mono'][self.languages[0]]['train'],
                                all_data['mono'][self.languages[1]]['train']]

        self.mono_data_valid = [all_data['mono'][self.languages[0]]['valid'],
                                all_data['mono'][self.languages[1]]['valid']]

        self.dictionary_lang1 = all_data['dico'][self.languages[0]]
        self.dictionary_lang2 = all_data['dico'][self.languages[1]]

        self.pad_index = self.dictionary_lang1.index(PAD_WORD)
        self.eos_index = self.dictionary_lang1.index(EOS_WORD)
        self.bos_index = self.dictionary_lang1.index(BOS_WORD)

        self.lang1_train_iterator = self.mono_data_train[0].get_iterator(shuffle=True,
                                                                            group_by_size=False)

        self.lang2_train_iterator = self.mono_data_train[1].get_iterator(shuffle=True,
                                                                            group_by_size=False)

        self.train_iterators = [self.lang1_train_iterator(), self.lang2_train_iterator()]

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
            if self.is_shared_emb and i>0:
                break

            # define dictionary / parameters to update
            dico = self.data['dico'][lang]

            # update the embedding layer of the encoder & decoder, for language i
            to_update = [self.encoder.embedding_layers[i].weight.data]
            to_update.append(self.decoder.embedding_layers[i].weight.data)
            to_update.append(self.linear_layers[i].weight.data)

            # for every word in that language
            for word_id in range(self.vocab_size):
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
            logger.info(
                "Initialized %i / %i word embeddings for \"%s\" (including %i "
                "after lowercasing)." % (_found, self.vocab_size, i, _lower)
            )

    def reconstruction_loss(self, orig, output):
        # TODO
        pass

    def enc_loss(self, orig, output):
        # TODO
        pass

    def generate_pairs(self):
        # TODO
        pass

    def beam_search(self):
        # TODO
        pass

    def label_smoothing(self):
        # TODO
        pass

    def get_src_mask(self, src_batch):
        mask = torch.ones_like(src_batch)
        mask.masked_fill_(src_batch == self.pad_index, 0)
        return mask

    def train_iter(self, src_batch, tgt_batch):

        self.forward(src_batch)

    def train_loop(self, train_iter):

        for i in range(train_iter):

            src_lan = i % 2
            tgt_lan = (i + 1) % 2
            src_batch = next(self.train_iterators[src_lan])
            tgt_batch = next(self.train_iterators[tgt_lan])
            print(src_batch)
            src_mask = self.get_src_mask(src_batch[0])
            print(src_mask)
            
if __name__ == "__main__":

    # test transformer
    x = torch.zeros(20, 5, dtype=torch.int64)
    y = torch.zeros(20, 7, dtype=torch.int64)
    tgt_m = np.tril(np.ones((1, 7, 7)), k=0).astype(np.uint8)
    tgt_m = torch.from_numpy(tgt_m)

    src_m = torch.zeros(20, 5).unsqueeze(-2).unsqueeze(-2)
    model = Transformer(n_langs=2)
    out = model(input_seq=x, prev_output=y, src_mask=src_m, tgt_mask=tgt_m, src_lang=1, tgt_lang=0)
    print(out.shape)

    parser=get_parser()
    data_params = parser.parse_args()
    check_all_data_params(data_params)
    model.load_data(data_params=data_params)
    print('loaded data')
    model.initialize_embeddings(embedding_file="corpora/mono/all.en-fr.60000.vec")
    print("initialized embeddings")
    model.train_loop(train_iter=1)






