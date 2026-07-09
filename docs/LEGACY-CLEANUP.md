# Legacy (V1) cleanup inventory

**Status:** planning document for a **separate, deliberate deletion PR**. Nothing here is deleted by
the doc-audit that produced this file. It is the evidence-backed map of which repo-root paths were
superseded by the **v2 substrate** at the 2026-07-09 cutover, and what in `v2/` replaces each.

## Context

Since the 2026-07-09 cutover (`main` @ `d115035`, PR #72), the production stack is defined and
operated **entirely from `v2/`**:

- **What runs:** `v2/out/docker-compose.yml` + `v2/out/.env`, *rendered* from the declarative source
  `v2/ordo.yaml` by the render engine (`python -m ordo.cli render`). `v2/out/` is **gitignored** — it
  is regenerated, never committed. The rendered compose runs under project name `ordo-v2`.
- **What does NOT run:** the repo-root `docker-compose.yml` (58 KB V1 file) and the V1 bring-up path
  (`./compose` / `.\compose.ps1`, `make up`, `overrides/compute.yml`). These are the legacy stack,
  kept as a rollback asset (V1 containers were removed at consolidation; **V1 volumes + images were
  retained**, so V1 is reconstitutable).

Evidence that the root compose is not the runtime:
- `v2/.gitignore` lists `out/` — the runtime compose/`.env` are rendered locally into `v2/out/`, not
  committed at the root.
- `v2/CUTOVER.md` → "CONSOLIDATION EXECUTED (2026-07-09)": "runtime copied to `v2/out/` at the
  primary checkout … re-rendered on-GPU (compose byte-identical)".
- `v2/PARITY.md` maps every root `docker-compose.yml` service to a v2 core service or `v2/plugins/*`
  manifest — the root compose is the *source* of the parity map, not the operated artifact.

> **Do not delete anything based on this file until a maintainer confirms no other consumer references
> the path.** Some root directories are still referenced by v2 as **build contexts** (see the "KEEP —
> referenced by v2 as a build context" section) and must NOT be removed.

---

## A. Dead legacy — superseded, safe to remove after confirmation

| Root path | Superseded by (evidence) | Notes |
|---|---|---|
| `ops-controller/` | **V2-native control plane.** The v2 image builds from `v2/docker/ops-controller.Dockerfile` (context `v2/`, `COPY ordo ./ordo` etc.) as `ordo-v2/ops-controller:latest` (`ordo serve`). The dashboard nginx proxies to this control plane (referred to as `ops-api`). | Explicitly flagged in `v2/CUTOVER.md` → "Cleanup candidates …": *"Dead legacy repo-root `ops-controller/` — the V2 `ops-api` builds from `docker/ops-api` … NOT from the repo-root `ops-controller/`. That top-level copy is unused legacy V1 code."* |
| `model-gateway/` | `v2/docker/model-gateway/` → `ordo-v2/model-gateway:latest` (LiteLLM + `local-chat` alias config-wrapper). | `v2/PARITY.md`: model-gateway is a "project buildable image … see `docker/model-gateway/`". Confirm no host-side tool references the root `litellm_config.yaml` before removing. |
| `docker-compose.yml` (root, V1) | `v2/out/docker-compose.yml`, rendered from `v2/ordo.yaml`. | This is the V1 stack definition. Keep as the parity reference / rollback map until v2 is fully trusted; it is **not** operated. Deleting it also lets the root `.env`/`overrides/` model retire. |
| `overrides/` (`compute.yml`, `gpu-assignments.yml`, `*.yml`) | The render engine + `ordo detect` (hardware) + the `ordo serve` scheduler (GPU pinning via `CUDA_VISIBLE_DEVICES` in the rendered compose). | V1's `COMPUTE_MODE` + hardware-detection-writes-an-override model. Superseded by declarative render. |
| `.env.example` (root, V1) | `v2/out/.env` is rendered; secret keys come from `v2/out/secrets.env.example`; host config from `v2/ordo.example.yaml`'s `site:` block. | Root `.env` is no longer the source of truth (config is rendered). |
| `dashboard/` (root V1 FastAPI + SPA) | The V1-parity dashboard was **reinstated on the v2 stack** as `ordo-v2/dashboard:latest` (built via `v2/docker/dashboard.Dockerfile`) with backend service `ops-api`. Confirm whether v2's dashboard build reuses this root context before removing. | **Verify before delete** — the v2 dashboard image may reference root `dashboard/` as its build context. If so, this belongs in section B, not A. |

## B. KEEP — referenced by v2 as a build context (do NOT remove)

These root directories are still the **single source of truth** for a v2 image's build context (v2
references them rather than duplicating, so they can't drift). Removing them would break `ordo`
image builds. See `v2/CUTOVER.md` §2 (build commands) for the exact references.

| Root path | Referenced by | Evidence |
|---|---|---|
| `rag-ingestion/` | `ordo-v2/rag-ingestion:latest` (rag plugin) | `v2/CUTOVER.md`: `docker build -t ordo-v2/rag-ingestion:latest C:/dev/ordo-ai-stack/rag-ingestion` |
| `worker/` | `ordo-v2/worker:latest` (media plugin) | `v2/CUTOVER.md`: `docker build -f …/worker/Dockerfile -t ordo-v2/worker:latest …` |
| `codebase-memory-ui/` | `ordo-v2/codebase-memory-ui:latest` (live service plugin) | `v2/CUTOVER.md`: `docker build -t ordo-v2/codebase-memory-ui:latest …/codebase-memory-ui` |
| `qdrant-rag-mcp/` | `ordo-v2/qdrant-rag-mcp:latest` (kind=mcp plugin) | `v2/CUTOVER.md`: `docker build -t ordo-v2/qdrant-rag-mcp:latest …/qdrant-rag-mcp` |
| `hermes/` | `ordo-v2/agent-hermes:latest` (default agent) | `v2/CUTOVER.md`: `docker build -t ordo-v2/agent-hermes:latest …/hermes` |
| `codebase-memory-mcp/` | the codebase-memory MCP image (gateway-spawned, registered in `v2/out/mcp-registry.yaml`) | still-live MCP; the image is a build step, not a compose service (`v2/PARITY.md`). |

## C. Verify disposition before acting (ambiguous — needs a maintainer decision)

| Root path | Question |
|---|---|
| `comfyui-mcp/` | `v2/PARITY.md` classes `comfyui-mcp` as gateway-spawned (tracked) and the `-image` builder as obsolete-as-a-service. Is the root `comfyui-mcp/` still the build context for the ComfyUI MCP image v2's gateway spawns? If yes → KEEP (section B). If the operator's ComfyUI image absorbed it → dead (section A). |
| `orchestration-mcp/` | `v2/PARITY.md` classes the orchestration-mcp image builder as obsolete-as-a-service (build step). Confirm whether v2 still builds this image from the root context (→ KEEP) or the orchestration verbs moved into another image (→ dead). |
| `auth/` (Caddy + oauth2-proxy config, certs, emails allowlist) | The v2 `edge` plugin (Caddy + oauth2-proxy, profile `edge`) is the front door. Does the rendered `edge` compose bind-mount config **from root `auth/`**, or does v2 carry its own copy? If it bind-mounts root `auth/`, KEEP. |
| `config/` | Determine what root `config/` holds and whether any v2-rendered service still mounts it. |
| `tests/` (root V1 pytest suite) | The v2 tests live under `v2/tests/` (run via the throwaway-container command). Is the root `tests/` suite still exercised by CI, or only the legacy V1 code? If only V1 code (dashboard/ops-controller/model-gateway root copies), it retires with them. |
| `scripts/` | V1 setup/ops scripts (`ensure_dirs`, `detect_hardware.py`, `compose` wrapper, pullers). Superseded by `ordo render`/`detect`/`fetch`. Some (e.g. `ssrf-egress-block.sh`) may still be invoked out-of-band — audit each script's callers before removing. |
| `mcp/` | The mcp-gateway config-wrapper image builds from `v2/docker/mcp-gateway/`. Confirm whether that context references root `mcp/gateway/` (wrapper + template) as its source; if so, KEEP the referenced files. |

## D. Operator-retained soak artifacts (outside the repo — operator deletes, not a repo PR)

From `v2/CUTOVER.md` → "Cleanup candidates …" (these are on-disk, not in git; listed for completeness):

- `C:\dev\ordo-ai-stack\data-v1-snapshot\`, `C:\dev\ordo-v2-data-retired\`, `C:\dev\ordo-v2-out-retired\`
  (the last contains the old `secrets.env` — **shred**, don't just recycle).
- **V1 volume/image prune** (after soak, operator runs — irreversible): remove the `ordo-ai-stack_*`
  volumes + `docker image prune -a`, **EXCEPT keep `ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470`**
  (shared with the running v2 llamacpp).

---

## Recommended order for the cleanup PR

1. Resolve every **section C** question (grep the repo + rendered v2 compose for each path as a build
   context or bind-mount source). Move each path to A (delete) or B (keep) with evidence.
2. Delete only **section A** paths confirmed to have no remaining consumer.
3. Update `docs/` + root `README.md` to drop the V1 pointers this audit added, once the V1 tree is
   gone.
4. Leave **section D** to the operator (on-disk, post-soak).
