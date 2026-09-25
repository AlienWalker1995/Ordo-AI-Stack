# ordo/evals - dependencies for the eval harness (services/evals). Code is NOT baked in.
#
# Build context is THIS folder:
#   ordo build evals   (tags ordo/evals:<commit>; see ordo/host/images.py)
#
# The tracked code and generic datasets are bind-mounted at /app by the plugin manifest
# (${BASE_PATH}/services/evals:/app:ro), so the image only has to change when requirements.txt does.
# That is deliberate: a baked copy is the stale-copy failure mode (the container keeps running old
# code after a merge until someone remembers to rebuild).

# Build stage: the one git dependency (instruction_following_eval, pinned by commit) needs git,
# which the runtime image does not carry.
# python:3.12-slim, pinned by DIGEST (resolved 2026-09-15).
FROM python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

FROM python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
COPY --from=build /opt/venv /opt/venv

# uid/gid 1000, NOT an arbitrary system uid: the runner reads Hermes's state.db from the
# hermes-home volume, whose root is `drwx------ 1000:1000` (the hermes user). Any other uid cannot
# traverse it even through a read-only mount.
RUN groupadd --gid 1000 evals && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/evals evals

ENV PATH=/opt/venv/bin:$PATH \
    HOME=/home/evals \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

USER 1000:1000
COPY build/prefetch.py /tmp/prefetch.py
RUN python /tmp/prefetch.py

# Everything an eval needs is baked above; a run must not reach the internet for data.
ENV HF_HUB_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1

WORKDIR /app
ENTRYPOINT ["python", "-m", "ordo_evals"]
CMD ["--help"]
