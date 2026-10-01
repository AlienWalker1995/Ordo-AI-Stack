# The NInfer weight converter (tools.convert) at the engine commit services/ninfer/Dockerfile pins,
# for CPU-only conversion of source checkpoints into .ninfer artifacts. Built and run by
# services/ninfer/convert/run.sh; see services/ninfer/README.md "Converting weights".
# Upstream's converter needs Python 3.11 with PyTorch and NumPy (docs/weight-conversion.md).

# python:3.11-slim
FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# CPU wheels only: the conversion never touches a GPU (run.sh starts it without one). Every
# package is pinned to an exact version in requirements.txt.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple -r /tmp/requirements.txt

# Must equal NINFER_COMMIT in services/ninfer/Dockerfile: an artifact is converted by the same
# engine revision that serves it. The build checks the fetched commit is exactly this sha.
ARG NINFER_COMMIT=d44ab58408aa389728cd8b1ee50179527e1f3e0d
WORKDIR /ninfer
RUN git init -q \
    && git remote add origin https://github.com/Neroued/ninfer \
    && git fetch -q --depth 1 origin "${NINFER_COMMIT}" \
    && git checkout -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${NINFER_COMMIT}"

COPY convert.sh /usr/local/bin/convert.sh
COPY inputs /inputs
ENTRYPOINT ["/bin/sh", "/usr/local/bin/convert.sh"]
