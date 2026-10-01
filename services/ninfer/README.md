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
