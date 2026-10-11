"""Which llama.cpp build runs the chat backend on this host: the ONE place that decides it.

(GPU vendor, NVIDIA compute capability, CPU arch) -> a llama.cpp backend and its pinned upstream
server image. The sizer (catalog.fits), the render (.env, -ngl, ctx sizing) and compose (image,
device wiring) all ask this module, so they cannot disagree about whether chat runs on the GPU.

Images are upstream ggml-org server builds, all at ONE build number, pinned by tag + index
digest (multi-arch indexes, so the same pin serves amd64 and arm64 where upstream publishes
both). Resolved read-only on 2026-10-11 with `docker buildx imagetools inspect`:

  server-b11552        amd64 arm64 s390x   CPU only (libggml-cpu-* variants, no GPU backend)
  server-cuda12-b11552 amd64 arm64         CUDA 12.8.1; PTX sm_50..sm_90, native sm_86/89/120a
  server-rocm-b11552   amd64               ROCm 7.2.1
  server-vulkan-b11552 amd64 arm64         Vulkan (Mesa), reaches the GPU through /dev/dri

The CPU pin is the same digest llamacpp-cpu and llamacpp-embed already run
(tests/substrate/test_llamacpp_backend.py holds the three together). Bump every backend to one
new build together, re-resolving each digest.

A catalog model may still pin a special build (`backend_image`, e.g. the first-party patched
sm_120-only image `ordo/llamacpp-patched`); it then declares `requires.min_compute_cap`, and catalog.fits keeps it off GPUs that
build cannot run on.
"""
from __future__ import annotations

import dataclasses

from .hardware import HardwareProfile, compute_cap_tuple


@dataclasses.dataclass(frozen=True)
class LlamaCppBackend:
    name: str                       # cpu | cuda | rocm | vulkan
    image: str                      # upstream server image, build tag + index digest
    devices: tuple[str, ...] = ()   # host device nodes compose passes through (non-NVIDIA GPUs)

    @property
    def accelerated(self) -> bool:
        """The model's layers go on the GPU (-ngl -1, VRAM-sized) rather than in RAM."""
        return self.name != "cpu"


CPU = LlamaCppBackend(
    "cpu",
    "ghcr.io/ggml-org/llama.cpp:server-b11552"
    "@sha256:fc9b506e89c1a1cfdd7bc056480f54dc657c1c04d76839a08642dc280b8ed2f6")
# NVIDIA goes through the compose `driver: nvidia` reservation (compose.py), not device nodes.
CUDA = LlamaCppBackend(
    "cuda",
    "ghcr.io/ggml-org/llama.cpp:server-cuda12-b11552"
    "@sha256:9d835c2febd6f09a0aafb30e9c1b29a67a49923b67075993551ab4593d84491f")
# Experimental: rendered and compose-validated, never run on real AMD hardware here.
ROCM = LlamaCppBackend(
    "rocm",
    "ghcr.io/ggml-org/llama.cpp:server-rocm-b11552"
    "@sha256:d44aa74ffda23aa5b31844f71aa001fe5eeba1d56d8b5c3155cc84fb94cd6823",
    devices=("/dev/kfd", "/dev/dri"))
# Experimental: rendered and compose-validated, never run on real hardware here.
VULKAN = LlamaCppBackend(
    "vulkan",
    "ghcr.io/ggml-org/llama.cpp:server-vulkan-b11552"
    "@sha256:f4e145aa5370fd619be9dc512e28a5bb55581dc48b957fe09f775e359f2c7ee8",
    devices=("/dev/dri",))

BACKENDS: dict[str, LlamaCppBackend] = {b.name: b for b in (CPU, CUDA, ROCM, VULKAN)}

# The oldest GPU the CUDA image has kernels for: ggml's default CMAKE_CUDA_ARCHITECTURES for a
# CUDA 12.x toolkit starts at 50-virtual (Maxwell). Older cards (Kepler) run the CPU build.
CUDA_MIN_COMPUTE_CAP = (5, 0)


def select(hw: HardwareProfile) -> tuple[LlamaCppBackend, list[str]]:
    """The backend for this host's primary GPU, plus operator-facing notes on the choice."""
    gpu = hw.primary_gpu
    if gpu is None:
        return CPU, []
    if gpu.vendor == "nvidia":
        compute_cap = compute_cap_tuple(gpu.compute_cap)
        if compute_cap is not None and compute_cap < CUDA_MIN_COMPUTE_CAP:
            return CPU, [f"{gpu.name} (compute capability {gpu.compute_cap}) is older than the CUDA "
                         f"build supports ({CUDA_MIN_COMPUTE_CAP[0]}.{CUDA_MIN_COMPUTE_CAP[1]}+); "
                         "llama.cpp runs on the CPU"]
        return CUDA, []
    if gpu.vendor == "amd":
        # Upstream publishes the ROCm server image for amd64 only; Vulkan covers arm64.
        backend = ROCM if hw.arch == "x86_64" else VULKAN
        return backend, [f"{gpu.name}: llama.cpp {backend.name} backend is EXPERIMENTAL (not yet run on "
                         f"AMD hardware); NVIDIA-only plugins stay off"]
    if gpu.vendor == "intel":
        return VULKAN, [f"{gpu.name}: llama.cpp vulkan backend is EXPERIMENTAL (not yet run on Intel "
                        "hardware); NVIDIA-only plugins stay off"]
    if gpu.vendor == "apple":
        return CPU, [f"{gpu.name}: Docker cannot reach Metal, so the in-stack llama.cpp runs on the CPU "
                     "(`ordo native` prints a Metal llama-server command instead)"]
    return CPU, [f"{gpu.name}: unknown GPU vendor {gpu.vendor!r}; llama.cpp runs on the CPU"]
