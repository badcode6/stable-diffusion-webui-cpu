import contextlib
import os

import numpy as np
import torch
from PIL import Image
from basicsr.utils.download_util import load_file_from_url
from tqdm import tqdm

from modules import modelloader
from modules.paths import models_path
from modules.shared import cmd_opts, opts, device
from modules.swinir_model_arch import SwinIR as net
from modules.upscaler import Upscaler, UpscalerData

precision_scope = (
    torch.autocast if cmd_opts.precision == "autocast" else contextlib.nullcontext
)


class UpscalerSwinIR(Upscaler):
    def __init__(self, dirname):
        self.name = "SwinIR"
        self.model_url = "https://github.com/JingyunLiang/SwinIR/releases/download/v0.0" \
                         "/003_realSR_BSRGAN_DFOWMFC_s64w8_SwinIR" \
                         "-L_x4_GAN.pth "
        self.model_name = "SwinIR 4x"
        self.model_path = os.path.join(models_path, self.name)
        self.user_path = dirname
        super().__init__()
        scalers = []
        model_files = self.find_models(ext_filter=[".pt", ".pth"])
        for model in model_files:
            if "http" in model:
                name = self.model_name
            else:
                name = modelloader.friendly_name(model)
            model_data = UpscalerData(name, model, self)
            scalers.append(model_data)
        self.scalers = scalers

    def do_upscale(self, img, model_file):
        model = self.load_model(model_file)
        if model is None:
            return img
        model = model.to(device)
        img = upscale(img, model)
        try:
            torch.cuda.empty_cache()
        except:
            pass
        return img

    def load_model(self, path, scale=4):
        if "http" in path:
            dl_name = "%s%s" % (self.model_name.replace(" ", "_"), ".pth")
            filename = load_file_from_url(url=path, model_dir=self.model_path, file_name=dl_name, progress=True)
        else:
            filename = path
        if filename is None or not os.path.exists(filename):
            return None
        model = net(
            upscale=scale,
            in_chans=3,
            img_size=64,
            window_size=8,
            img_range=1.0,
            depths=[6, 6, 6, 6, 6, 6, 6, 6, 6],
            embed_dim=240,
            num_heads=[8, 8, 8, 8, 8, 8, 8, 8, 8],
            mlp_ratio=2,
            upsampler="nearest+conv",
            resi_connection="3conv",
        )

        pretrained_model = torch.load(filename)
        model.load_state_dict(pretrained_model["params_ema"], strict=True)
        if not cmd_opts.no_half:
            model = model.half()
        return model


def upscale(
        img,
        model,
        tile=opts.SWIN_tile,
        tile_overlap=opts.SWIN_tile_overlap,
        window_size=8,
        scale=4,
):
    img = np.array(img)
    img = img[:, :, ::-1]
    img = np.moveaxis(img, 2, 0) / 255
    img = torch.from_numpy(img).float()
    img = img.unsqueeze(0).to(device)
    with torch.no_grad(), precision_scope("cpu"):
        _, _, h_old, w_old = img.size()
        h_pad = (h_old // window_size + 1) * window_size - h_old
        w_pad = (w_old // window_size + 1) * window_size - w_old
        img = torch.cat([img, torch.flip(img, [2])], 2)[:, :, : h_old + h_pad, :]
        img = torch.cat([img, torch.flip(img, [3])], 3)[:, :, :, : w_old + w_pad]
        output = inference(img, model, tile, tile_overlap, window_size, scale)
        output = output[..., : h_old * scale, : w_old * scale]
        output = output.data.squeeze().float().cpu().clamp_(0, 1).numpy()
        if output.ndim == 3:
            output = np.transpose(
                output[[2, 1, 0], :, :], (1, 2, 0)
            )  # CHW-RGB to HCW-BGR
        output = (output * 255.0).round().astype(np.uint8)  # float32 to uint8
        return Image.fromarray(output, "RGB")


def inference(img, model, tile, tile_overlap, window_size, scale):
    # test the image tile by tile
    b, c, h, w = img.size()
    tile = min(tile, h, w)
    assert tile % window_size == 0, "tile size should be a multiple of window_size"
    sf = scale

    stride = tile - tile_overlap
    h_idx_list = list(range(0, h - tile, stride)) + [h - tile]
    w_idx_list = list(range(0, w - tile, stride)) + [w - tile]
    E = torch.zeros(b, c, h * sf, w * sf, dtype=torch.half, device=device).type_as(img)
    W = torch.zeros_like(E, dtype=torch.half, device=device)

    with tqdm(total=len(h_idx_list) * len(w_idx_list), desc="SwinIR tiles") as pbar:
        for h_idx in h_idx_list:
            for w_idx in w_idx_list:
                in_patch = img[..., h_idx: h_idx + tile, w_idx: w_idx + tile]
                out_patch = model(in_patch)
                out_patch_mask = torch.ones_like(out_patch)

                E[
                ..., h_idx * sf: (h_idx + tile) * sf, w_idx * sf: (w_idx + tile) * sf
                ].add_(out_patch)
                W[
                ..., h_idx * sf: (h_idx + tile) * sf, w_idx * sf: (w_idx + tile) * sf
                ].add_(out_patch_mask)
                pbar.update(1)
    output = E.div_(W)

    return output
