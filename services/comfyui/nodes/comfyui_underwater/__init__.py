"""Underwater color-correction node for ComfyUI (GPU hybrid).

Physics-motivated restoration (NOT diffusion): recovers attenuated red
wavelengths, removes the blue/green cast and milky haze, keeps a natural
scuba look. Designed for VIDEO with temporally-smoothed white balance.

Performance (per pedantic audit): the earlier "GPU is slow" result was an
OOM/thrash artifact of benchmarking with llamacpp resident. Run clean, the
hybrid below is ~10-25x faster than the CPU/cv2 path at 4K:
  - white balance (shades-of-gray p-norm) and saturation: hand-written torch
    (the p-norm is the CPU pipeline's dominant cost; trivial on GPU);
  - dehaze: kornia rgb_to_lab -> equalize_clahe(L) -> lab_to_rgb (already fast
    and numerically correct on GPU);
  - small chunks, NO per-chunk empty_cache (that ~2x'd runtime).

TEMPORAL EMA PERSISTENCE: VHS Batch Manager re-instantiates the node every
window, so instance/local EMA state would reset each window and cause periodic
white-balance pulsing. We persist EMA in a module-level store so smoothing is
continuous across windows (and across chaptered files of one dive, which keeps
concat seams continuous). `reset_temporal` clears it to start a fresh sequence.
"""

import torch

try:
    import kornia
    _HAS_KORNIA = True
except Exception:
    _HAS_KORNIA = False

# Persists across VHS Batch Manager re-instantiations (one video at a time).
_TEMPORAL = {"ema": None}


class UnderwaterColorCorrect:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "wb_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05}),
                "norm_power": ("INT", {"default": 6, "min": 1, "max": 12}),
                "dehaze_clip": ("FLOAT", {"default": 2.5, "min": 0.0, "max": 40.0, "step": 0.5}),
                "saturation": ("FLOAT", {"default": 1.15, "min": 0.5, "max": 2.0, "step": 0.05}),
                "contrast": ("FLOAT", {"default": 0.02, "min": 0.0, "max": 0.1, "step": 0.005}),
                "temporal_smooth": ("FLOAT", {"default": 0.9, "min": 0.0, "max": 0.99, "step": 0.01}),
                "max_gain": ("FLOAT", {"default": 3.5, "min": 1.0, "max": 8.0, "step": 0.1}),
                "preserve_luminance": ("BOOLEAN", {"default": True}),
                "chunk_size": ("INT", {"default": 8, "min": 1, "max": 64}),
                "reset_temporal": ("BOOLEAN", {"default": False}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "correct"
    CATEGORY = "image/underwater"

    def correct(self, images, wb_strength, norm_power, dehaze_clip, saturation,
                contrast, temporal_smooth, max_gain, preserve_luminance,
                chunk_size, reset_temporal):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if reset_temporal:
            _TEMPORAL["ema"] = None
        B = images.shape[0]
        out = torch.empty_like(images)                       # (B,H,W,3) CPU
        luma = torch.tensor([0.299, 0.587, 0.114], device=device).view(1, 3, 1, 1)
        use_clahe = _HAS_KORNIA and dehaze_clip > 0

        for s in range(0, B, chunk_size):
            e = min(s + chunk_size, B)
            c = images[s:e].to(device, non_blocking=True).permute(0, 3, 1, 2).contiguous().clamp(0, 1)

            # 1. shades-of-gray WB gains (hand-written), per-frame EMA (persistent)
            x = c + 1e-6
            norm = x.pow(norm_power).mean(dim=(2, 3)).pow(1.0 / norm_power)   # (b,3)
            gray = norm.mean(dim=1, keepdim=True)
            gains = (gray / norm).clamp(0.4, max_gain)                        # (b,3)

            ema = _TEMPORAL["ema"]
            sm = torch.empty_like(gains)
            for i in range(gains.shape[0]):
                gi = gains[i:i + 1]
                ema = gi if ema is None else (temporal_smooth * ema + (1.0 - temporal_smooth) * gi)
                sm[i] = ema
            _TEMPORAL["ema"] = ema.detach()

            # 2. apply gains, preserve luminance
            applied = (1.0 + wb_strength * (sm - 1.0)).view(-1, 3, 1, 1)
            wb = c * applied
            if preserve_luminance:
                om = c.mean(dim=(1, 2, 3), keepdim=True)
                nm = wb.mean(dim=(1, 2, 3), keepdim=True) + 1e-6
                wb = wb * (om / nm)
            wb = wb.clamp(0, 1)

            # 3. dehaze: CLAHE on L (kornia)
            if use_clahe:
                lab = kornia.color.rgb_to_lab(wb)
                L = (lab[:, 0:1] / 100.0).clamp(0, 1)
                L = kornia.enhance.equalize_clahe(L, clip_limit=float(dehaze_clip), grid_size=(8, 8))
                wb = kornia.color.lab_to_rgb(torch.cat([L * 100.0, lab[:, 1:]], dim=1)).clamp(0, 1)

            # 4. saturation (hand-written luma formula)
            if abs(saturation - 1.0) > 1e-3:
                g = (wb * luma).sum(dim=1, keepdim=True)
                wb = (g + saturation * (wb - g)).clamp(0, 1)

            # 5. percentile contrast stretch (estimated on a strided sample)
            if contrast > 0:
                samp = wb[:, :, ::8, ::8].reshape(-1)
                lo = torch.quantile(samp, contrast)
                hi = torch.quantile(samp, 1.0 - contrast)
                if (hi - lo) > 1e-4:
                    wb = ((wb - lo) / (hi - lo)).clamp(0, 1)

            out[s:e] = wb.permute(0, 2, 3, 1).contiguous().to(out.dtype).cpu()
            del c, wb

        return (out,)


NODE_CLASS_MAPPINGS = {"UnderwaterColorCorrect": UnderwaterColorCorrect}
NODE_DISPLAY_NAME_MAPPINGS = {"UnderwaterColorCorrect": "Underwater Color Correct"}
