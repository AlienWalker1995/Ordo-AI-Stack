# syntax=docker/dockerfile:1.6
# Custom llama.cpp server built from the AmesianX TurboQuant fork.
#
# Ships turbo2 (2.5 bpw) and turbo3 (3.5 bpw) KV-cache quantization types
# on top of mainline llama.cpp. See docs/configuration.md for the operator
# reference and the plan note at the bottom of this file for rationale.
#
# Pinned commit: AmesianX/TurboQuant @ 0a8062ded5a4a3e050102874bf21a3e5303a95cf
#                                      (HEAD of main as of 2026-04-22)
# To bump: `git ls-remote https://github.com/AmesianX/TurboQuant.git HEAD`,
# update TURBOQUANT_SHA below, rebuild. Re-run the perplexity sanity check
# after any bump — the fork can rebase onto mainline without warning.
#
# Target GPU: RTX 5090 (Blackwell, compute capability 12.0 = sm_120).
# Building for sm_120 explicitly avoids a PTX JIT stall on the first call.

# CUDA 12.8+ is required for Blackwell sm_120 (RTX 50-series consumer) —
# earlier versions' nvcc rejects `-gencode=arch=compute_120` with
# "Unsupported gpu architecture 'compute_120'".
ARG CUDA_VERSION=12.8.1
ARG UBUNTU_VERSION=22.04

# --- Build stage --------------------------------------------------------------
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION} AS build

ARG TURBOQUANT_REPO=https://github.com/AmesianX/TurboQuant.git
ARG TURBOQUANT_SHA=0a8062ded5a4a3e050102874bf21a3e5303a95cf
ARG CMAKE_CUDA_ARCHITECTURES=120

ENV DEBIAN_FRONTEND=noninteractive

# CUDA driver API stubs — libcuda.so.1 is provided by the NVIDIA driver at
# runtime (via --gpus all + nvidia-container-runtime), but at build time the
# linker needs a stub to satisfy cuMemCreate / cuMemMap / etc references
# emitted by ggml-cuda.
#
# The toolkit ships /usr/local/cuda/lib64/stubs/libcuda.so (unversioned), but
# libggml-cuda.so is built with SONAME dependency on libcuda.so.1 — so when
# linking the executable we need both LIBRARY_PATH AND a versioned symlink.
# The symlink is created below in the apt-install step; LIBRARY_PATH lets gcc
# pass the stubs dir to ld via its generated -L flags.
ENV LIBRARY_PATH=/usr/local/cuda/lib64/stubs

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        libcurl4-openssl-dev \
        ninja-build \
        pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1

WORKDIR /src
# Fetch only the specific commit tree (no history, no submodules — the fork's
# .gitmodules is empty as of the pinned SHA). Avoids the `--filter=blob:none`
# lazy-fetch path which stalled during builds in this environment.
RUN git init . \
    && git remote add origin "${TURBOQUANT_REPO}" \
    && git -c protocol.version=2 fetch --depth 1 origin "${TURBOQUANT_SHA}" \
    && git checkout --detach FETCH_HEAD

# Flags chosen for this deployment:
#   GGML_CUDA=ON                    enable CUDA backend
#   CMAKE_CUDA_ARCHITECTURES=120    Blackwell sm_120 (RTX 5090) — no PTX JIT
#   GGML_CUDA_FA_ALL_QUANTS=ON      compile Flash Attention for every quant
#                                   variant. TurboQuant silently corrupts
#                                   without FA, so this + the shell-wrapper
#                                   safety rail must both be in place.
#   GGML_NATIVE=OFF                 don't tune to host CPU; we're CUDA-bound.
#   LLAMA_BUILD_TESTS=OFF           skip fork test suite; we test at the
#                                   llama-server arg + runtime level.
# (LLAMA_CURL is deprecated in current llama.cpp — curl is enabled by default.)
RUN cmake -S . -B build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="${CMAKE_CUDA_ARCHITECTURES}" \
        -DGGML_CUDA_FA_ALL_QUANTS=ON \
        -DGGML_NATIVE=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
    && cmake --build build -j --target llama-server llama-perplexity

# --- Runtime stage ------------------------------------------------------------
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}

ARG TURBOQUANT_SHA=0a8062ded5a4a3e050102874bf21a3e5303a95cf
LABEL org.opencontainers.image.source="https://github.com/AmesianX/TurboQuant"
LABEL org.opencontainers.image.revision="${TURBOQUANT_SHA}"
LABEL org.opencontainers.image.description="llama.cpp server w/ TurboQuant KV-cache (turbo2/turbo3) for Blackwell sm_120"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the built binaries plus every ggml / llama shared lib they dlopen.
# COPY with a trailing slash pulls the whole build/bin directory — simpler
# than enumerating every libggml-*.so variant, and works whether the fork
# builds them as unversioned or versioned (libfoo.so.N) symlinks.
COPY --from=build /src/build/bin/ /app/

ENV LD_LIBRARY_PATH=/app:${LD_LIBRARY_PATH}

EXPOSE 8080

# Intentionally empty — the compose service mounts
# scripts/llamacpp/run-llama-server.sh as the entrypoint so the stack's existing
# env-driven arg assembly (with the turbo* -> --flash-attn on safety rail) stays
# authoritative. See docker-compose.yml for the `entrypoint:` override.
ENTRYPOINT []
CMD ["/app/llama-server", "--host", "0.0.0.0", "--port", "8080"]
