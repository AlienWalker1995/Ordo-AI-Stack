# tts-lexicon: the pronunciation lexicon in front of the text-to-speech engine (lexicon_proxy.py).
#
# Build context is THIS folder:
#   ordo build tts-lexicon   (tags ordo/tts-lexicon:<commit>; see ordo/host/images.py)
#
# python:3.12-slim, pinned by DIGEST (the same base the gpu-gate runs on): this image sits in the
# request path of every spoken sentence.
FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

# aiohttp is the only dependency: the proxy relays the engine's streamed audio as it arrives, which
# stdlib http.server cannot do. Pinned exactly, to the version the gpu-gate and the tests run.
RUN pip install --no-cache-dir aiohttp==3.14.3

WORKDIR /app
COPY lexicon_proxy.py /app/lexicon_proxy.py
COPY kokoro_vocab.json /app/kokoro_vocab.json

# Unprivileged: the proxy holds no secrets, touches no Docker socket and reserves no GPU.
RUN useradd --system --uid 10001 --no-create-home lexicon
USER 10001

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/lexicon_proxy.py"]
