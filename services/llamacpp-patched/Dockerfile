# nvidia/cuda:12.8.0-devel-ubuntu22.04
FROM nvidia/cuda@sha256:54f18e2a8e1b3d03f77b9a6dc905533da46ac93a5513f10e8ba8e560db9fa5ab AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git curl libgomp1 python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Pinned to the stable release v0.5.0 (build b11146, 2026-09-23) for a reproducible build. It carries
# Qwen3.8-Flash-Next support (qwen4exp, ggml-org#27742) and CUDA sparse FA for it (ggml-org#28770).
# Do NOT float on master: bump only after re-verifying both patches still apply (the build fails
# loudly if they don't) and re-benchmarking the active model's decode speed.
RUN git init -q && git remote add origin https://github.com/ggml-org/llama.cpp \
    && git fetch -q --depth 1 origin 7fe450e19305b828c199d602c23a8337aaa1f03b \
    && git checkout -q FETCH_HEAD \
    && git submodule update --init --recursive --depth 1

# The server embeds a web UI whose required files change between releases. Left to itself the
# build fetches it from a Hugging Face bucket named after the build number, which a shallow clone
# reports as 1 ("b1" does not exist), so it falls back to the floating "latest" UI; once that UI
# moved past this commit the embed step failed ("missing required asset(s): loading.html").
# Pin the UI to the release built from the pinned commit (b11146), verified by sha256. Assets in
# tools/ui/dist take priority, and LLAMA_USE_PREBUILT_UI=OFF below removes the network fallback.
# The test below names files scripts/ui-assets.cmake requires at this commit (loading.html is gone).
ARG LLAMA_UI_RELEASE=b11146
ARG LLAMA_UI_SHA256=832be73b1d077980b047ddd2212f8a8a229f7c007a03ce032ea06448734ea6f2
RUN curl -fsSL -o /tmp/llama-ui.tar.gz \
        "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_UI_RELEASE}/llama-${LLAMA_UI_RELEASE}-ui.tar.gz" \
    && echo "${LLAMA_UI_SHA256}  /tmp/llama-ui.tar.gz" | sha256sum -c - \
    && mkdir -p tools/ui/dist \
    && tar -xzf /tmp/llama-ui.tar.gz -C tools/ui/dist --strip-components=1 \
    && test -f tools/ui/dist/index.html && test -f tools/ui/dist/sw.js && test -f tools/ui/dist/_app/version.json \
    && rm /tmp/llama-ui.tar.gz

# =============================================================================
# PATCH 1: fix checkpoint search for hybrid/recurrent models
#   upstream issues: #22384, #20225, #24055
#   Still needed at v0.5.0: upstream only added a `pos_max > pos_next` guard; its pos_min test
#   still rejects the checkpoints of a hybrid model, and #24055 is open.
# =============================================================================
RUN python3 - <<'PYEOF'
import sys
path = "tools/server/server-context.cpp"
with open(path, "r") as f:
    src = f.read()

old = "return cur.pos_min < pos_min_thold || cur.pos_min == 0;"
new = (
    "if (llama_model_is_recurrent(model_tgt) || llama_model_is_hybrid(model_tgt)) {\n"
    "                                                return cur.pos_max <= pos_next;\n"
    "                                            }\n"
    "                                            return cur.pos_min < pos_min_thold || cur.pos_min == 0;"
)

count = src.count(old)
if count != 1:
    print(f"PATCH 1 FAILED: expected 1 occurrence, found {count}", file=sys.stderr)
    sys.exit(1)

src = src.replace(old, new, 1)
with open(path, "w") as f:
    f.write(src)
print("PATCH 1: hybrid/recurrent checkpoint search fix (ggml-org#22384)")
PYEOF

# =============================================================================
# PATCH 2: recurrent_shrink/expand API for prompt cache operations
#   (upstream PR #24785, without the now-redundant needs_reeval workaround —
#    upstream commit b9180 already has GDN partial rollback via n_rs_seq)
#   Still needed at v0.5.0: #24785 is unmerged and llama.h has no recurrent shrink/expand API.
# =============================================================================
COPY pr24785-minimal.diff /tmp/pr24785-minimal.diff
RUN cd /src && git apply --recount --check /tmp/pr24785-minimal.diff && echo "Minimal diff applies cleanly"
RUN cd /src && git apply --recount /tmp/pr24785-minimal.diff && echo "Minimal diff applied"

ENV CUDAToolkit_ROOT=/usr/local/cuda
ENV PATH=/usr/local/cuda/bin:$PATH
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/lib/libcuda.so.1
# Blackwell (sm_120) only: the catalog gates every model that pins this image on min_compute_cap 12.0.
# CMAKE_CUDA_ARCHITECTURES is the variable ggml-cuda reads (ggml/src/ggml-cuda/CMakeLists.txt); unset,
# it falls back to a ~9-architecture list. ggml rewrites "120" to "120a" for the FP4 tensor-core kernels.
RUN cmake -B build \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES="120" \
    -DGGML_LTO=ON \
    -DGGML_CUDA_FA_ALL_QUANTS=ON \
    -DGGML_SHARED=OFF \
    -DBUILD_SHARED_LIBS=OFF \
    -DGGML_BUILD_TESTS=OFF \
    -DGGML_BUILD_BENCHMARKS=OFF \
    -DLLAMA_BUILD_SERVER=ON \
    -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_USE_PREBUILT_UI=OFF \
    -DGGML_NATIVE=ON \
    -DCMAKE_EXE_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -Wl,-rpath-link,/usr/local/cuda/lib64/stubs"
RUN cmake --build build --config Release --target llama-server llama-quantize -j $(nproc)
RUN mkdir -p /src/build/so-libraries && \
    find /src/build/bin /src/build/lib -name "lib*.so*" -exec cp -t /src/build/so-libraries/ {} + 2>/dev/null || true

# nvidia/cuda:12.8.0-runtime-ubuntu22.04
FROM nvidia/cuda@sha256:b0e5afeabc356a9c41148c50f219d3117d46cb6ec23c590e3a0ebaa1db0ff946
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Our stack's entrypoint (scripts/llamacpp/run-llama-server.sh) does `exec /app/llama-server`,
# so the patched binary must live at /app to be a drop-in for ghcr.io/ggml-org/llama.cpp.
COPY --from=builder /src/build/bin/llama-server /app/llama-server
COPY --from=builder /src/build/bin/llama-quantize /usr/local/bin/llama-quantize
RUN ln -sf /app/llama-server /usr/local/bin/llama-server
COPY --from=builder /src/build/so-libraries/ /usr/local/lib/
RUN ldconfig
EXPOSE 8080 8084
ENTRYPOINT ["llama-server"]
CMD ["--host", "0.0.0.0", "--port", "8080"]