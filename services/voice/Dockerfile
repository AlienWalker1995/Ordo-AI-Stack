# syntax=docker/dockerfile:1.6
# Voice TTS for the Ordo stack: Resemble AI's Chatterbox Turbo (MIT, 350M) behind devnen's
# Chatterbox-TTS-Server, which speaks the OpenAI /v1/audio/speech API. A request picks a voice by
# file name in `voice` and Chatterbox clones it zero-shot: one of the bundled voices, or a clip the
# operator adds under data/voice/references.
#
# It runs on the secondary GPU, the Pascal GTX 1070 (compute 6.1): the 5090 belongs to the LLM and
# the GPU lease, and speech must never evict it. Pascal is why torch is 2.5.1+cu121 (its wheels
# still carry sm_61 kernels) and why everything runs in FP32. Measured there, warm: a short reply in
# about 0.8 s, real-time factor 0.54-0.73, about 2 GB of VRAM.
#
# Everything is pinned: the base image by digest, the server and the Chatterbox fork by commit, and
# every Python package by exact version (requirements.lock, installed with --no-deps).
FROM docker.io/nvidia/cuda:12.8.1-runtime-ubuntu22.04@sha256:4a801ef9232d2b05e69df4eb8aa054dbbe2824e5499e1e6e857320bb01ac41a9

ARG SERVER_REPO=https://github.com/devnen/Chatterbox-TTS-Server.git
ARG SERVER_COMMIT=915ae289340e10c6047f27f47e22eae9bf350c32
ARG CHATTERBOX_REPO=https://github.com/devnen/chatterbox-v2.git
ARG CHATTERBOX_COMMIT=cc0357396d9c73fc1e6c544ee40bb596020edd09

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/app/hf_cache

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev build-essential git ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN git clone "$SERVER_REPO" /app && git -C /app checkout --quiet "$SERVER_COMMIT" \
    && rm -rf /app/.git
WORKDIR /app

COPY requirements.lock /tmp/requirements.lock
RUN pip3 install --no-cache-dir --upgrade "pip==25.3" \
    && pip3 install --no-cache-dir --no-deps -r /tmp/requirements.lock \
    && pip3 install --no-cache-dir --no-deps "git+${CHATTERBOX_REPO}@${CHATTERBOX_COMMIT}" \
    && python3 -c "import torch, chatterbox; assert torch.__version__ == '2.5.1+cu121', torch.__version__"

# The stack's settings: Turbo on CUDA, the bundled voices plus the operator's clips (mounted at
# reference_audio/), Emily.wav as the default. Replaces the server's own config.yaml.
COPY config.yaml /app/config.yaml
RUN mkdir -p model_cache reference_audio outputs voices logs hf_cache

# start.sh runs the server and, beside it, sends two warm-up requests once the model is loaded.
COPY start.sh /app/start.sh
RUN chmod 0755 /app/start.sh

EXPOSE 8004
CMD ["/app/start.sh"]
