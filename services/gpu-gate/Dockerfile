# gpu-gate — the generic GPU admission gate.
#
# Build context is THIS folder:
#   docker build -f services/gpu-gate/Dockerfile -t ordo/gpu-gate:latest services/gpu-gate
# Resolved for preflight + tests via compose.SUBSTRATE_BUILD_CONTEXTS (ordo/buildspec.py), like
# the other substrate images that compose renders without a plugin manifest.
#
# python:3.12-slim, pinned by DIGEST — a rolling tag here is exactly the silent drift the stack
# bans, and this image sits in the request path of every render submission.
FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

# aiohttp is the only dependency: the gate must relay ComfyUI's /ws progress stream as well as
# plain HTTP, which stdlib http.server cannot do. Pinned exactly (no range) — one moving part in
# the GPU admission path is one too many.
RUN pip install --no-cache-dir aiohttp==3.14.3

WORKDIR /app
COPY gate.py /app/gate.py

# Unprivileged: the gate holds no secrets beyond a bearer token, touches no Docker socket, and
# reserves no GPU. It is an HTTP proxy that talks to the arbiter.
RUN useradd --system --uid 10001 --no-create-home gate
USER 10001

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/gate.py"]
