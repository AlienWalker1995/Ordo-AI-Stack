# html-video: HTML/CSS/SVG animations -> RGBA video, rendered by headless Chromium. CPU only.
#
# Build context is THIS folder:
#   ordo build html-video   (tags ordo/html-video:<commit>; see ordo/host/images.py)
#
# Base: Microsoft's Playwright image for Python, pinned by DIGEST. It ships Ubuntu 24.04, Python
# 3.12 and the browser builds under /ms-playwright, but NOT the `playwright` Python package: the
# package version below MUST match the image's browser revision (v1.63.0 -> chromium-1243), which
# is why both are pinned to the same release and bumped together.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0

ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Exact pins (no ranges):
#   playwright  - the browser driver; same release as the base image's browsers.
#   aiohttp     - the HTTP server (the version the gate and the test suite run).
#   jinja2      - the card templates (autoescaped: callers send data, never markup).
#   av          - PyAV, a wheel with FFmpeg bundled: encodes the frames to PNG-in-MOV (RGBA).
#                 Bundling it in the wheel keeps the encoder pinned with pip rather than apt.
#   numpy/pillow - screenshot PNG -> RGBA array for the encoder.
RUN pip install \
      playwright==1.63.0 \
      aiohttp==3.14.3 \
      jinja2==3.1.6 \
      av==19.0.1 \
      numpy==2.5.3 \
      pillow==12.3.0

WORKDIR /app
COPY app.py cards.py kit.py /app/
COPY templates /app/templates
COPY assets /app/assets

# Unprivileged: the image's `pwuser` (uid 1001). Chromium runs without its setuid sandbox (it
# needs CAP_SYS_ADMIN, which the stack does not grant); the renderer only ever loads pages it
# generated itself and blocks every request that is not for its own assets, so the sandbox
# would guard against nothing this service does.
USER pwuser

EXPOSE 8000
ENTRYPOINT ["python3", "/app/app.py"]
