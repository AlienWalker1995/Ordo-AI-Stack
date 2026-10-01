# NInfer engine image

This build context produces the first-party image `ordo/ninfer`: the
[NInfer](https://github.com/Neroued/ninfer) inference engine (Apache-2.0), the `ninfer` CLI and the
OpenAI-compatible `ninfer-serve` server, at `/usr/local/bin`. A catalog model that runs on NInfer
names it untagged as its `backend_image`, and render fills in the tag `ordo build` recorded.

## Pins
- **Engine:** upstream commit `d44ab58408aa389728cd8b1ee50179527e1f3e0d` (2026-09-29), fetched by
  sha inside the build (`NINFER_COMMIT`). It is the commit the model eval built and served. Bump it
  only after re-running the eval; the build checks that the fetched commit is exactly this sha.
- **Base images:** `nvidia/cuda:13.0.2-devel-ubuntu24.04` and `-runtime-ubuntu24.04`, each pinned by
  its index digest.
- **GPU:** sm_120a only (RTX 50 series). Upstream's CMakeLists refuses any other architecture.

## Deviation from upstream: CUDA 13.0, not 13.1
Upstream validates CUDA 13.1 (its Dockerfile uses `nvidia/cuda:13.1.2-*`). This image uses 13.0.2
because the host driver stays on the 581.x branch on purpose, and 581.x supports CUDA 13.0 at most:
a 13.1 container does not start on it. Upstream sets no CUDA version floor at this commit, and the
eval built and served it under 13.0.2. Move to upstream's toolkit when the driver branch moves.

The other differences from upstream's Dockerfile: the source is fetched by commit instead of
`COPY . .`, and the compile runs at most `NINFER_BUILD_JOBS` (8) parallel jobs to keep the build
host's power draw modest.

## Build
From the repo root:
```
ordo build ninfer
```
That builds `ordo/ninfer:<sha12>` (the commit that last changed this folder), moves
`ordo/ninfer:current` and records the tag in `out/images.json`. A full compile takes a while (it
is a CUDA build). `ordo up` and `ordo apply` also build it when the rendered compose names a tag
Docker lacks. Never `docker build` it by hand: a hand tag is invisible to the stack.

## Converting weights
`convert/` converts a source checkpoint into a `.ninfer` artifact with upstream's own converter
(`python -m tools.convert`, [docs/weight-conversion.md](https://github.com/Neroued/ninfer/blob/d44ab58408aa389728cd8b1ee50179527e1f3e0d/docs/weight-conversion.md))
at the same engine commit this image serves, on the CPU only. Each artifact is two files in
`convert/inputs/`:
- `<name>.sources`: every input file with its sha256 and its source URL at a pinned revision;
- `<name>.args`: the converter arguments, one per line.

```
services/ninfer/convert/run.sh <name> <work dir> <out dir>
```
`run.sh` downloads the inputs into `<work dir>` on the host (resumable; nothing is converted unless
every file matches its sha256), builds `ordo/ninfer-convert:<engine sha12>` (Python 3.11, CPU
PyTorch, every package pinned in `convert/requirements.txt`) and runs the converter with no GPU and
at most `CONVERT_CPUS` (12) CPUs. The artifact and its `.sha256` land in `<out dir>`.

The converter writes a random artifact id into every file, so a re-run produces the same weights
under a different sha256. The catalog pins the sha256 of the file that was converted, validated
and published, not of a re-run.

| Artifact | Source | Recipe | Notes |
|---|---|---|---|
| `qwen3.8-27b-heretic-ara` | `heretic-org/Qwen3.8-27B-heretic-ara` | `qwen3_8_27b` (groupwise-int) | Text, Vision, MTP and the proposal head; official `Qwen/Qwen3.8-27B` frontend. |

Why groupwise-int and not NVFP4 for the Heretic weights: upstream's NVFP4 recipe imports
pre-quantized NVFP4/FP8 weights (`--source quantized`, compressed-tensors in the mixed layout of
`unsloth/Qwen3.8-27B-NVFP4`), and no such quantization of these weights exists. The published
Heretic `.ninfer` files do not load on this engine: both are v2 files, which it refuses, and
upstream's v2-to-v3 upgrade refuses both (a custom model identity, and an object inventory that
matches no official artifact).
