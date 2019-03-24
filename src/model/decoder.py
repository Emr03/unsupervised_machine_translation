import math

from src.model.sublayers import *


class DecoderLayer(torch.nn.Module):

    def __init__(self, params):

        super(DecoderLayer, self).__init__()
        self.d_model = params["d_model"]
        self.masked_attn = SelfAttention(params)
        self.attn = SelfAttention(params)
        self.ffnn = FFNN(params)
        self.layer_norm_1 = torch.nn.LayerNorm(normalized_shape=self.d_model)
        self.layer_norm_2 = torch.nn.LayerNorm(normalized_shape=self.d_model)
        self.layer_norm_3 = torch.nn.LayerNorm(normalized_shape=self.d_model)
        self.dropout = torch.nn.Dropout(params["dropout"])

    def forward(self, prev_outputs, enc_outputs, src_mask, tgt_mask):

        out = self.layer_norm_1(self.dropout(self.masked_attn(x_q=dec_outputs,
                                                 x_k=dec_outputs,
                                                 x_v=dec_outputs,
                                                 mask=tgt_mask)) + dec_outputs)

        out = self.layer_norm_2(self.dropout(self.attn(x_q=out,
                                          x_k = enc_outputs,
                                          x_v = enc_outputs,
                                          mask=src_mask)) + out)

        out = self.layer_norm_3(self.dropout(self.ffnn(out)) + out)
        return out

class StackedDecoder(torch.nn.Module):

    def __init__(self, n_layers, params, vocab_size, n_langs, is_shared_emb=True, freeze_emb=True):

        super(StackedDecoder, self).__init__()
        self.d_model = params["d_model"]
        self.vocab_size = vocab_size
        self.n_langs = n_langs
        emb_scale = torch.tensor([math.sqrt(self.d_model)])
        self.register_buffer('emb_scale', emb_scale)
        embd_layer = torch.nn.Embedding(self.vocab_size[0], self.d_model)

        if is_shared_emb:
            self.embedding_layers = torch.nn.ModuleList([embd_layer for _ in range(self.n_langs)])

        else:
            self.embedding_layers = torch.nn.ModuleList([torch.nn.Embedding(self.vocab_size[l], self.d_model) for l in range(self.n_langs)])

        # freeze embedding layers
        if freeze_emb:
            for l in self.embedding_layers:
                l.weight.requires_grad = False

        self.pos_enc = PositionalEncoding(params)
        self.decoder_layers = torch.nn.ModuleList([DecoderLayer(params) for _ in range(n_layers)])

    def forward(self, prev_outputs, enc_outputs, src_mask, tgt_mask, lang_id):
        """

        :param dec_outputs: in case of inference: words generated so far
                            in case of training: target sentence
        :param enc_outputs: latent vectors generated by encoder
        :param mask:
        :return:
        """
        prev_outputs = self.emb_scale * self.embedding_layers[lang_id](prev_outputs)
        prev_outputs = self.pos_enc(prev_outputs)
        for layer in self.decoder_layers:
            dec_outputs = layer(prev_outputs=prev_outputs,
                                enc_outputs=enc_outputs,
                                src_mask = src_mask,
                                tgt_mask = tgt_mask)

        return dec_outputs

if __name__ == "__main__":
    from src.utils.config import params
    # test decoder layer
    x = torch.zeros(20, 5, 512, dtype=torch.float32)
    tgt_m = torch.zeros(1, 5)
    tgt_m[:, 0] = 1
    src_m = torch.ones(20, 5)
    src_m[:, -2:-1] = 0
    src_m = src_m.unsqueeze(-2).unsqueeze(-2)
    dec_layer = DecoderLayer(params)
    out = dec_layer(dec_outputs=x, enc_outputs=x, src_mask=src_m,  tgt_mask=tgt_m)
    print(out.shape)

    # test decoder stack
    x = torch.zeros(20, 5, dtype=torch.int64)
    y = torch.zeros(20, 5, 512, dtype=torch.float32)
    dec = StackedDecoder(n_layers=6, vocab_size=[90, 90], params=params, n_langs=2, is_shared_emb=False)
    out = dec(x, y, src_mask=src_m,  tgt_mask=tgt_m, lang_id=0)
    print(out.shape)
