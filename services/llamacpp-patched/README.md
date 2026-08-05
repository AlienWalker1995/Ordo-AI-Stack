# Patched llama.cpp build (Qwen3.6 SWA / hybrid-cache)

The stock `ghcr.io/ggml-org/llama.cpp:server` image cannot load the Qwen3.6 hybrid
attention/SSM model — it needs two out-of-tree patches. This build context produces the
image the `huihui-qwen3.6-27b-q6` catalog entry pins via `backend_image`:

```
ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470
```

The `86b9470` suffix is the pinned upstream commit (`86b94708…`) the patches were tested
against — a reproducible build, not a floating `:server` tag.

## What's patched
- **PATCH 1** — hybrid/recurrent checkpoint-search fix (upstream ggml-org#22384, #20225, #24055).
- **PATCH 2** — `recurrent_shrink/expand` prompt-cache API (upstream PR #24785, minimal diff in
  `pr24785-minimal.diff`). The build **fails loudly** if either patch stops applying — that's
  the signal to re-verify before bumping the pinned commit.

## Build
```
docker build -t ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470 services/llamacpp-patched
```

This image is **local-only** — it is not published to any registry, so `ordo preflight`
reports a missing one as "build from services/llamacpp-patched", not "Docker will pull".

## Files
- `Dockerfile` — two-stage CUDA build (12.8 devel → runtime), pinned commit + both patches.
- `pr24785-minimal.diff` — PATCH 2 source.

(An old `launch.txt` flag snapshot was removed 2026-08-05: it predated the render substrate
and contradicted the live config on every distinguishing flag — `-c 196608` vs the deployed
131072, an MTP `--spec-type` the flag builder strips as inert, a model no longer in the
catalog. The launch surface is owned by `catalog/models.yaml` + `services/ops-api/llamacpp_flags.py`
+ `scripts/llamacpp/run-llama-server.sh`; tuning rationale belongs in the catalog entry comments.)
