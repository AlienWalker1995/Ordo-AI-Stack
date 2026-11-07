# NInfer (github.com/Neroued/ninfer, Apache-2.0): the first-party image `ordo/ninfer`, built by
# `ordo build ninfer`. Upstream's own Dockerfile at the pinned commit, with three deliberate changes:
#   1. The source is fetched by full commit sha inside the build, not COPYed from a local checkout.
#   2. The CUDA base images are 13.0.2, pinned by index digest, not upstream's floating 13.1.2 tags.
#      The host driver stays on the 581.x branch on purpose, and 581.x supports CUDA 13.0 at most, so
#      a 13.1 container does not start there. Upstream validates 13.1 but sets no CUDA version floor
#      (its README: "CMake does not impose a CUDA version floor"); this commit built and served under
#      13.0.2 in the model eval. Revisit when the driver branch moves.
#   3. The compile is capped at NINFER_BUILD_JOBS parallel jobs instead of one per core, to keep the
#      build host's power draw modest.

# nvidia/cuda:13.0.2-devel-ubuntu24.04
FROM nvidia/cuda:13.0.2-devel-ubuntu24.04@sha256:5dc1bca23d05bd37b011be68ec470c03b403a5da07ec3a86e41af9470e9d0cc6 AS build

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        cmake \
        git \
        libavcodec-dev \
        libavformat-dev \
        libavutil-dev \
        libcurl4-openssl-dev \
        libhwloc-dev \
        libjpeg-dev \
        libswscale-dev \
        liburing-dev \
        ninja-build \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Pinned to an exact upstream commit (2026-11-07, "fix(runtime): recover preempted requests without
# stalling"). Since the previous pin (d44ab584, 2026-09-29) upstream added native Prometheus /metrics,
# DFlash2 and unified startup configuration. Never float on a branch: bump the sha deliberately and
# re-run the eval. Third-party code is vendored in the repo (third_party/), so the build fetches
# nothing else.
ARG NINFER_COMMIT=77c2e5e273b53843f0564fb53b7bad249e7c85c0
WORKDIR /src
RUN git init -q \
    && git remote add origin https://github.com/Neroued/ninfer \
    && git fetch -q --depth 1 origin "${NINFER_COMMIT}" \
    && git checkout -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${NINFER_COMMIT}"

# Link against the toolkit's driver stub, as upstream does: the build stage has no host GPU driver.
RUN ln -s /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/libcuda.so.1

# The RTX 5090 target; upstream's other target is DGX Spark, which this host is not. The target
# implies sm_120a and cmake refuses any other architecture for it, so the explicit value only makes
# the image's GPU requirement visible here.
ARG NINFER_BUILD_JOBS=8
RUN cmake -S . -B /build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DNINFER_TARGET=rtx5090 \
        -DCMAKE_CUDA_ARCHITECTURES=120a \
        -DNINFER_BUILD_APPS=ON \
        -DBUILD_TESTING=OFF \
        -DNINFER_BUILD_BENCHMARKS=OFF \
    && cmake --build /build --parallel "${NINFER_BUILD_JOBS}" --target ninfer ninfer-serve

# nvidia/cuda:13.0.2-runtime-ubuntu24.04
FROM nvidia/cuda:13.0.2-runtime-ubuntu24.04@sha256:6a0e31b59e70890446f2c17356d6efc0d54260090a8c63d6ca7c2ad049db95d2

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        libavcodec60 \
        libavformat60 \
        libavutil58 \
        libcurl4t64 \
        libhwloc15 \
        libjpeg-turbo8 \
        libswscale7 \
        liburing2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /build/apps/ninfer /usr/local/bin/ninfer
COPY --from=build /build/apps/ninfer-serve /usr/local/bin/ninfer-serve
# The engine's own shared libraries: upstream split ops and core into libraries (35aede69).
COPY --from=build /build/lib/ /usr/local/lib/
RUN ldconfig

WORKDIR /workspace
EXPOSE 8080
STOPSIGNAL SIGTERM

CMD ["ninfer-serve", "--help"]
