"""Bouncing-DVD-logo overlay for a batch of video frames.

Resizes the logo once, then walks (x, y) with a constant pixel-per-second velocity,
reflecting off the four edges. Alpha-composites the logo onto each frame at the
requested opacity. Works on the standard ComfyUI IMAGE shape [B, H, W, C] in
float 0-1.

Writes back into the input batch in place (and returns it) so we don't carry a
duplicate 17 GB-class tensor for the rest of the workflow.
"""

import gc
import math

import numpy as np
import torch
from PIL import Image


def _tensor_to_pil(img_tensor):
    arr = (img_tensor.clamp(0, 1).cpu().numpy() * 255.0).astype(np.uint8)
    if arr.shape[-1] == 4:
        return Image.fromarray(arr, mode="RGBA")
    return Image.fromarray(arr, mode="RGB")


def _pil_to_tensor_rgb(pil_img):
    if pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")
    arr = np.asarray(pil_img, dtype=np.float32) / 255.0
    return torch.from_numpy(arr)


class OrdoDVDLogoOverlay:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "logo": ("IMAGE",),
                "fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.01}),
                "logo_scale": ("FLOAT", {"default": 0.12, "min": 0.02, "max": 0.5, "step": 0.01,
                                          "tooltip": "Logo width as a fraction of frame width."}),
                "opacity": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 1.0, "step": 0.01}),
                "speed_pps": ("FLOAT", {"default": 120.0, "min": 0.0, "max": 2000.0, "step": 1.0,
                                         "tooltip": "Pixels per second the logo travels."}),
                "start_x_frac": ("FLOAT", {"default": 0.1, "min": 0.0, "max": 1.0, "step": 0.01}),
                "start_y_frac": ("FLOAT", {"default": 0.1, "min": 0.0, "max": 1.0, "step": 0.01}),
                "angle_deg": ("FLOAT", {"default": 30.0, "min": 0.0, "max": 360.0, "step": 1.0,
                                         "tooltip": "Initial direction of travel."}),
            },
            "optional": {
                "logo_mask": ("MASK", {"tooltip": "Optional alpha mask for the logo (wire LoadImage's MASK output here so transparency is honored)."}),
                "bypass": ("BOOLEAN", {"default": False,
                                       "tooltip": "True = skip logo overlay and pass frames through unchanged."}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "overlay"
    CATEGORY = "Ordo/LastMile"
    DESCRIPTION = "Composite a logo over every frame with DVD-screensaver bouncing motion."

    def overlay(self, images, logo, fps=30.0, logo_scale=0.12, opacity=0.3,
                speed_pps=120.0, start_x_frac=0.1, start_y_frac=0.1, angle_deg=30.0,
                logo_mask=None, bypass=False):
        if bypass:
            return (images,)
        if images.ndim != 4:
            raise ValueError(f"images must be [B,H,W,C], got shape {tuple(images.shape)}")
        B, H, W, C = images.shape

        # Logo: take the first frame of whatever batch was passed in.
        logo_pil = _tensor_to_pil(logo[0])
        if logo_pil.mode != "RGBA":
            logo_pil = logo_pil.convert("RGBA")

        # ComfyUI's LoadImage splits RGB and alpha — IMAGE is RGB-only, MASK
        # holds the alpha. If the caller wired the mask in, replace the logo's
        # opaque-everywhere alpha with the real transparency. MASK shape is
        # [B,H,W] in float 0–1 where 1 = transparent (LoadImage inverts it).
        if logo_mask is not None and logo_mask.numel() > 0:
            m = logo_mask[0] if logo_mask.ndim == 3 else logo_mask
            mask_np = m.clamp(0, 1).cpu().numpy()
            # LoadImage convention: mask is 1 where transparent → invert to get opacity
            alpha_np = (255.0 * (1.0 - mask_np)).astype(np.uint8)
            alpha_pil = Image.fromarray(alpha_np, mode="L")
            if alpha_pil.size != logo_pil.size:
                alpha_pil = alpha_pil.resize(logo_pil.size, Image.LANCZOS)
            r, g, b, _ = logo_pil.split()
            logo_pil.close()
            logo_pil = Image.merge("RGBA", (r, g, b, alpha_pil))

        target_w = max(1, int(W * logo_scale))
        target_h = max(1, int(target_w * logo_pil.height / max(1, logo_pil.width)))
        logo_pil = logo_pil.resize((target_w, target_h), Image.LANCZOS)

        # Apply user opacity by scaling the alpha channel.
        if 0.0 <= opacity < 1.0:
            r, g, b, a = logo_pil.split()
            a = a.point(lambda v: int(v * opacity))
            logo_pil = Image.merge("RGBA", (r, g, b, a))

        # Initial position and velocity (pixels per frame).
        px = start_x_frac * (W - target_w)
        py = start_y_frac * (H - target_h)
        per_frame = speed_pps / max(1e-6, fps)
        rad = math.radians(angle_deg)
        vx = math.cos(rad) * per_frame
        vy = math.sin(rad) * per_frame

        # In-place write into the input batch — see module docstring.
        out = images
        max_x = max(0, W - target_w)
        max_y = max(0, H - target_h)

        for i in range(B):
            frame_pil = _tensor_to_pil(images[i]).convert("RGBA")
            frame_pil.alpha_composite(logo_pil, dest=(int(px), int(py)))
            out[i] = _pil_to_tensor_rgb(frame_pil)
            frame_pil.close()
            if (i + 1) % 100 == 0:
                gc.collect()

            px += vx
            py += vy
            if px <= 0 and vx < 0:
                px = 0
                vx = -vx
            elif px >= max_x and vx > 0:
                px = max_x
                vx = -vx
            if py <= 0 and vy < 0:
                py = 0
                vy = -vy
            elif py >= max_y and vy > 0:
                py = max_y
                vy = -vy

        return (out,)
