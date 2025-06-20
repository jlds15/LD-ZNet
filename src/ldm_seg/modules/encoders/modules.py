import torch
import torch.nn as nn
from functools import partial
import clip
from einops import rearrange, repeat
from transformers import CLIPTokenizer, CLIPTextModel, T5Tokenizer, T5EncoderModel
import kornia

from ldm_seg.modules.x_transformer import (
    Encoder,
    TransformerWrapper,
)  # TODO: can we directly rely on lucidrains code and simply add this as a reuirement? --> test
from omegaconf import OmegaConf
from ldm_seg.util import instantiate_from_config


class AbstractEncoder(nn.Module):
    def __init__(self):
        super().__init__()

    def encode(self, *args, **kwargs):
        raise NotImplementedError


class ClassEmbedder(nn.Module):
    def __init__(self, embed_dim, n_classes=1000, key="class"):
        super().__init__()
        self.key = key
        self.embedding = nn.Embedding(n_classes, embed_dim)

    def forward(self, batch, key=None):
        if key is None:
            key = self.key
        # this is for use in crossattn
        c = batch[key][:, None]
        c = self.embedding(c)
        return c


class TransformerEmbedder(AbstractEncoder):
    """Some transformer encoder layers"""

    def __init__(self, n_embed, n_layer, vocab_size, max_seq_len=77, device="cuda"):
        super().__init__()
        self.device = device
        self.transformer = TransformerWrapper(
            num_tokens=vocab_size,
            max_seq_len=max_seq_len,
            attn_layers=Encoder(dim=n_embed, depth=n_layer),
        )

    def forward(self, tokens):
        tokens = tokens.to(self.device)  # meh
        z = self.transformer(tokens, return_embeddings=True)
        return z

    def encode(self, x):
        return self(x)


class BERTTokenizer(AbstractEncoder):
    """Uses a pretrained BERT tokenizer by huggingface. Vocab size: 30522 (?)"""

    def __init__(self, device="cuda", vq_interface=True, max_length=77):
        super().__init__()
        from transformers import BertTokenizerFast  # TODO: add to reuquirements

        self.tokenizer = BertTokenizerFast.from_pretrained("bert-base-uncased")
        self.device = device
        self.vq_interface = vq_interface
        self.max_length = max_length

    def forward(self, text):
        batch_encoding = self.tokenizer(
            text,
            truncation=True,
            max_length=self.max_length,
            return_length=True,
            return_overflowing_tokens=False,
            padding="max_length",
            return_tensors="pt",
        )
        tokens = batch_encoding["input_ids"].to(self.device)
        return tokens

    @torch.no_grad()
    def encode(self, text):
        tokens = self(text)
        if not self.vq_interface:
            return tokens
        return None, None, [None, None, tokens]

    def decode(self, text):
        return text


class BERTEmbedder(AbstractEncoder):
    """Uses the BERT tokenizr model and add some transformer encoder layers"""

    def __init__(
        self,
        n_embed,
        n_layer,
        vocab_size=30522,
        max_seq_len=77,
        device="cuda",
        use_tokenizer=True,
        embedding_dropout=0.0,
    ):
        super().__init__()
        self.use_tknz_fn = use_tokenizer
        if self.use_tknz_fn:
            self.tknz_fn = BERTTokenizer(vq_interface=False, max_length=max_seq_len)
        self.device = device
        self.transformer = TransformerWrapper(
            num_tokens=vocab_size,
            max_seq_len=max_seq_len,
            attn_layers=Encoder(dim=n_embed, depth=n_layer),
            emb_dropout=embedding_dropout,
        )

    def forward(self, text):
        if self.use_tknz_fn:
            tokens = self.tknz_fn(text)  # .to(self.device)
        else:
            tokens = text
        z = self.transformer(tokens, return_embeddings=True)
        return z

    def encode(self, text):
        # output of length 77
        return self(text)


class SpatialRescaler(nn.Module):
    def __init__(
        self,
        n_stages=1,
        method="bilinear",
        multiplier=0.5,
        in_channels=3,
        out_channels=None,
        bias=False,
    ):
        super().__init__()
        self.n_stages = n_stages
        assert self.n_stages >= 0
        assert method in [
            "nearest",
            "linear",
            "bilinear",
            "trilinear",
            "bicubic",
            "area",
        ]
        self.multiplier = multiplier
        self.interpolator = partial(torch.nn.functional.interpolate, mode=method)
        self.remap_output = out_channels is not None
        if self.remap_output:
            print(
                f"Spatial Rescaler mapping from {in_channels} to {out_channels} channels after resizing."
            )
            self.channel_mapper = nn.Conv2d(in_channels, out_channels, 1, bias=bias)

    def forward(self, x):
        for stage in range(self.n_stages):
            x = self.interpolator(x, scale_factor=self.multiplier)

        if self.remap_output:
            x = self.channel_mapper(x)
        return x

    def encode(self, x):
        return self(x)


class FrozenCLIPEmbedder(AbstractEncoder):
    """Uses the CLIP transformer encoder for text (from Hugging Face)"""

    def __init__(
        self, version="openai/clip-vit-large-patch14", device="cuda", max_length=77
    ):
        super().__init__()
        self.tokenizer = CLIPTokenizer.from_pretrained(version)
        self.transformer = CLIPTextModel.from_pretrained(version)
        self.device = device
        self.max_length = max_length
        self.freeze()

    def freeze(self):
        self.transformer = self.transformer.eval()
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, text):
        batch_encoding = self.tokenizer(
            text,
            truncation=True,
            max_length=self.max_length,
            return_length=True,
            return_overflowing_tokens=False,
            padding="max_length",
            return_tensors="pt",
        )
        tokens = batch_encoding["input_ids"].to(self.device)
        outputs = self.transformer(input_ids=tokens)

        z = outputs.last_hidden_state
        return z

    def encode(self, text):
        return self(text)


class FrozenT5Embedder(AbstractEncoder):
    """Uses the T5-XXL transformer encoder for text (from Hugging Face)"""

    def __init__(self, device="cuda", max_length=77):
        super().__init__()
        self.tokenizer = T5Tokenizer.from_pretrained("google/t5-v1_1-xl")
        self.transformer = T5EncoderModel.from_pretrained("google/t5-v1_1-xl")
        self.device = device
        self.max_length = max_length
        self.freeze()

    def freeze(self):
        self.transformer = self.transformer.eval()
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, text):
        with torch.no_grad():
            # batch_encoding = self.tokenizer(text, truncation=True, max_length=self.max_length, return_length=True, return_overflowing_tokens=False, padding="max_length", return_tensors="pt")
            batch_encoding = self.tokenizer(text, return_tensors="pt")
            tokens = batch_encoding["input_ids"].to(self.device)
            outputs = self.transformer(input_ids=tokens)

            z = outputs.last_hidden_state
        return z

    def encode(self, text):
        return self(text)


class FrozenCLIPTextEmbedder(nn.Module):
    """
    Uses the CLIP transformer encoder for text.
    """

    def __init__(
        self,
        version="ViT-L/14",
        device="cuda",
        max_length=77,
        n_repeat=1,
        normalize=True,
    ):
        super().__init__()
        self.model, _ = clip.load(version, jit=False, device="cpu")
        self.device = device
        self.max_length = max_length
        self.n_repeat = n_repeat
        self.normalize = normalize

    def freeze(self):
        self.model = self.model.eval()
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, text):
        tokens = clip.tokenize(text).to(self.device)
        z = self.model.encode_text(tokens)
        if self.normalize:
            z = z / torch.linalg.norm(z, dim=1, keepdim=True)
        return z

    def encode(self, text):
        z = self(text)
        if z.ndim == 2:
            z = z[:, None, :]
        z = repeat(z, "b 1 d -> b k d", k=self.n_repeat)
        return z


class FrozenClipImageEmbedder(nn.Module):
    """
    Uses the CLIP image encoder.
    """

    def __init__(
        self,
        model,
        jit=False,
        device="cuda" if torch.cuda.is_available() else "cpu",
        antialias=False,
    ):
        super().__init__()
        self.model, _ = clip.load(name=model, device=device, jit=jit)

        self.antialias = antialias

        self.register_buffer(
            "mean", torch.Tensor([0.48145466, 0.4578275, 0.40821073]), persistent=False
        )
        self.register_buffer(
            "std", torch.Tensor([0.26862954, 0.26130258, 0.27577711]), persistent=False
        )

    def preprocess(self, x):
        # normalize to [0,1]
        x = kornia.geometry.resize(
            x,
            (224, 224),
            interpolation="bicubic",
            align_corners=True,
            antialias=self.antialias,
        )
        x = (x + 1.0) / 2.0
        # renormalize according to clip
        x = kornia.enhance.normalize(x, self.mean, self.std)
        return x

    def forward(self, x):
        # x is assumed to be in range [-1,1]
        return self.model.encode_image(self.preprocess(x))


class FrozenSDUNet(AbstractEncoder):
    """Uses the CLIP transformer encoder for text (from Hugging Face)"""

    def __init__(
        self,
        path="models/ldm/stable-diffusion-v1/sd-v1-4-full-ema.ckpt",
        config=None,
        timesteps=None,
    ):
        super().__init__()
        self.config = OmegaConf.load(f"{config}")
        self.ckpt_path = path
        self.timesteps = timesteps
        self.model = self.load_model_from_config()
        del self.model.first_stage_model, self.model.cond_stage_model
        self.freeze()

    def load_model_from_config(self, verbose=False):
        config = self.config
        ckpt = self.ckpt_path
        print(f"Loading model from {ckpt}")
        pl_sd = torch.load(ckpt, map_location="cpu")
        if "global_step" in pl_sd:
            print(f"Global Step: {pl_sd['global_step']}")
        sd = pl_sd["state_dict"]
        model = instantiate_from_config(config.model)
        m, u = model.load_state_dict(sd, strict=False)
        if len(m) > 0 and verbose:
            print("missing keys:")
            print(m)
        if len(u) > 0 and verbose:
            print("unexpected keys:")
            print(u)

        # model.model = model.model.to(self.device) # only transfer the diffusion model to gpu
        return model

    def freeze(self):
        self.model = self.model.eval()
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, z, c, timestep=None):
        with self.model.ema_scope():
            if timestep is None:
                t = torch.randint(
                    self.timesteps[0],
                    self.timesteps[-1],
                    (z.shape[0],),
                    device=z.device,
                ).long()
            else:
                t = torch.randint(
                    timestep, timestep + 1, (z.shape[0],), device=z.device
                ).long()
            noise = torch.randn_like(z)
            z_noisy = self.model.q_sample(x_start=z, t=t, noise=noise)
            z_recon, all_features = self.model.apply_model(z_noisy, t, c)
            return all_features
        # with self.model.ema_scope():
        #     noise = torch.randn_like(z)
        #     features = {}
        #     for i, timestep in enumerate(self.timesteps):
        #         t = torch.randint(timestep, timestep+1, (z.shape[0],), device=z.device).long()
        #         z_noisy = self.model.q_sample(x_start=z, t=t, noise=noise)
        #         z_recon, all_features = self.model.apply_model(z_noisy, t, c)
        #         all_features_dict = {j:[feature] for j, feature in enumerate(all_features)}
        #         if i==0:
        #             for k in all_features_dict.keys():
        #                 features[k] = []
        #         for k in all_features_dict.keys():
        #             features[k] = [torch.cat(all_features_dict[k]+features[k], 1)]

        #     features = list(features.values())
        #     features = [f[0] for f in features]
        #     return features

    def encode(self, z, c, timestep=None):
        return self(z, c, timestep=timestep)


if __name__ == "__main__":
    from ldm.util import count_params

    model = FrozenCLIPEmbedder()
    count_params(model, verbose=True)
