# ltx-trainer — the official Lightricks LTX-2/2.3 trainer (github.com/Lightricks/LTX-2,
# packages/ltx-trainer) as a stack service image. Headless CLI: the container idles and every
# GPU run (preprocess/train) is exec'd through the lease-exec seam declared in the plugin
# manifest, so nothing here can touch the card without an ops-controller lease.
#
# Everything is pinned: the upstream commit (LTX2_SHA), uv, and the repo's committed uv.lock
# (torch 2.9.1 cu128 wheels — Blackwell/5090-capable). Bump LTX2_SHA deliberately and rebuild:
#   docker build -f docker/ltx-trainer.Dockerfile -t ordo/ltx-trainer:<sha12> docker
FROM nvidia/cuda:12.8.1-base-ubuntu24.04

ARG LTX2_SHA=9377758131b1ffde4b7f766804590a6617bf2ab9
ARG UV_VERSION=0.11.29

# ffmpeg: shared libs for torchcodec/av at runtime + the CLI the preprocessing scripts shell to.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ffmpeg python3.12 python3.12-dev python3-pip ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --break-system-packages --no-cache-dir "uv==${UV_VERSION}"

RUN git clone https://github.com/Lightricks/LTX-2 /app/LTX-2 \
    && git -C /app/LTX-2 checkout --detach "${LTX2_SHA}"

WORKDIR /app/LTX-2
# Workspace sync from the committed lock. ltx-kernels is workspace-excluded upstream, so no
# CUDA toolchain is needed at build time (int8-quanto training doesn't use it).
RUN uv sync --frozen --package ltx-trainer --python /usr/bin/python3.12 && uv cache clean

ENV VIRTUAL_ENV=/app/LTX-2/.venv \
    PATH=/app/LTX-2/.venv/bin:${PATH} \
    HF_HOME=/root/.cache/huggingface \
    PYTHONUNBUFFERED=1

WORKDIR /app/LTX-2/packages/ltx-trainer
CMD ["sleep", "infinity"]
