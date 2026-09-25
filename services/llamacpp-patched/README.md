# Patched llama.cpp build (Qwen3.6 SWA / hybrid-cache)

The stock `ghcr.io/ggml-org/llama.cpp:server` image cannot load the Qwen3.6 hybrid
attention/SSM model: it needs two out-of-tree patches. This build context produces the
first-party image `ordo/llamacpp-patched`, which the catalog entries that need it name (untagged)
via `backend_image`.

The Dockerfile pins the upstream commit (`86b94708…`) the patches were tested against, so the
build is reproducible, not a floating `:server` tag. Like every first-party image, it is tagged
with the commit that last changed this folder, recorded in `out/images.json`, and render pins
the compose to that tag.

The embedded web UI is pinned the same way: the release built from that commit (`b9843`),
downloaded by sha256. Upstream's default fetch resolves a shallow clone to build `b1`, which does
not exist, and falls back to the floating `latest` UI; a newer UI broke the embed step of this
commit. Bump `LLAMA_UI_RELEASE`/`LLAMA_UI_SHA256` together with the pinned commit.

## What's patched
- **PATCH 1** — hybrid/recurrent checkpoint-search fix (upstream ggml-org#22384, #20225, #24055).
- **PATCH 2** — `recurrent_shrink/expand` prompt-cache API (upstream PR #24785, minimal diff in
  `pr24785-minimal.diff`). The build **fails loudly** if either patch stops applying — that's
  the signal to re-verify before bumping the pinned commit.

## Build
From the repo root, while the active model uses this build:
```
ordo build llamacpp
python -m ordo --source out/ordo.yaml render --out out
ordo recreate llamacpp        # outside a GPU lease; it refuses during one
```
`ordo up` also builds it when Docker lacks the tag the compose names. It is a CUDA compile, so
the first build takes a while. Never `docker build` it by hand: a hand tag is invisible to the
stack. The image is local-only (no registry), so `ordo preflight` reports a missing one as
"build from services/llamacpp-patched", not "Docker will pull".

## Files
- `Dockerfile` — two-stage CUDA build (12.8 devel → runtime), pinned commit + both patches.
- `pr24785-minimal.diff` — PATCH 2 source.

(An old `launch.txt` flag snapshot was removed 2026-08-05: it predated the render substrate
and contradicted the live config on every distinguishing flag — `-c 196608` vs the deployed
131072, an MTP `--spec-type` the flag builder strips as inert, a model no longer in the
catalog. The launch surface is owned by `catalog/models.yaml` + `scripts/llamacpp/run-llama-server.sh`; tuning rationale belongs in the catalog entry comments.)
