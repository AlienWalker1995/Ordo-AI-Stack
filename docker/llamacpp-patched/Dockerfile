FROM nvidia/cuda:12.8.0-devel-ubuntu22.04 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git curl libgomp1 python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Pinned to the commit the SWA/hybrid-cache patches were tested against (2026-06-30)
# for a reproducible build. Do NOT float on master — bump only after re-verifying
# both patches still apply (the build fails loudly if they don't).
RUN git init -q && git remote add origin https://github.com/ggml-org/llama.cpp \
    && git fetch -q --depth 1 origin 86b94708f22478f900b76ca02e316f4f3418faff \
    && git checkout -q FETCH_HEAD \
    && git submodule update --init --recursive --depth 1

# =============================================================================
# PATCH 1: fix checkpoint search for hybrid/recurrent models
#   upstream issues: #22384, #20225, #24055
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
# =============================================================================
COPY pr24785-minimal.diff /tmp/pr24785-minimal.diff
RUN cd /src && git apply --recount --check /tmp/pr24785-minimal.diff && echo "Minimal diff applies cleanly"
RUN cd /src && git apply --recount /tmp/pr24785-minimal.diff && echo "Minimal diff applied"

ENV CUDAToolkit_ROOT=/usr/local/cuda
ENV PATH=/usr/local/cuda/bin:$PATH
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/lib/libcuda.so.1
RUN cmake -B build \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_CUDA=ON \
    -DGGML_CUDA_ARCHITECTURES="12.0" \
    -DGGML_LTO=ON \
    -DGGML_CUDA_FA_ALL_QUANTS=ON \
    -DGGML_SHARED=OFF \
    -DBUILD_SHARED_LIBS=OFF \
    -DGGML_BUILD_TESTS=OFF \
    -DGGML_BUILD_BENCHMARKS=OFF \
    -DLLAMA_BUILD_SERVER=ON \
    -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_CURL=ON \
    -DGGML_NATIVE=ON \
    -DCMAKE_EXE_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -Wl,-rpath-link,/usr/local/cuda/lib64/stubs"
RUN cmake --build build --config Release --target llama-server llama-quantize -j $(nproc)
RUN mkdir -p /src/build/so-libraries && \
    find /src/build/bin /src/build/lib -name "lib*.so*" -exec cp -t /src/build/so-libraries/ {} + 2>/dev/null || true

FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04
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