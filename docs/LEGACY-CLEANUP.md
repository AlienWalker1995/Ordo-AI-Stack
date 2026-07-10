# Legacy (V1) cleanup inventory

**Status:** planning document for a **separate, deliberate deletion PR**. Nothing here is deleted by
the doc-audit that produced this file. It is the evidence-backed map of which repo-root paths were
superseded by the **v2 substrate** at the 2026-07-09 cutover, and what in `v2/` replaces each.

> **AUDIT UPDATE (2026-07-10 — legacy-cleanup audit pass).** Every **section C** ambiguity below is
> now resolved with evidence (see the updated section C). The audit's load-bearing finding: **the
> entire V1 tree is one interlocked unit, not a set of independently-removable leaves.** The root
> `tests/` suite — run by the **non-path-gated** CI `pytest` job on every push/PR to `main`
> (`.github/workflows/ci.yml`) — *imports and asserts against* `ops-controller/main.py`
> (`tests/conftest.py`, `tests/test_comfyui_restart_harden.py`), `comfyui-mcp/managers/…`
> (`tests/test_comfyui_*`), `dashboard/` (`from dashboard …`), and the root `docker-compose.yml`
> (`tests/test_compose_smoke.py`). The same CI job **ruff-lints** `dashboard tests model-gateway
> ops-controller rag-ingestion scripts comfyui-mcp orchestration-mcp worker`. And root
> `docker-compose.yml` is the **build context** for `ops-controller/ model-gateway/ comfyui-mcp/
> orchestration-mcp/ dashboard/ mcp/ auth/` and the documented, intentional **V1 rollback asset**
> (README banner + `v2/CUTOVER.md`; V1 volumes+images KEPT for reconstitution; soak began 2026-07-09).
>
> **Consequence for the deletion PR:** none of section A is a *no-op* deletion. Removing any A item
> requires, **in the same PR**, (1) deleting/retiring the coupled root `tests/` cases, (2) removing
> the corresponding paths from **both** ci.yml ruff+path-filter jobs, and (3) deleting the root
> `docker-compose.yml` that gives them a build context — i.e. **retiring V1 wholesale**. That is a
> behavior-changing refactor gated on a maintainer's "V1 soak is over, retire it" decision, **not a
> safe-tier purge.** This audit therefore **executed no code/config deletions** (nothing qualified as
> provably-unreferenced-and-deletion-is-a-no-op); it only resolves the section-C ambiguities and
> records the coupling. The obviously-dead root cruft (`dist.tar.gz`, `final.html`, `index.html`,
> `tmp/`, `tmpnzf8l06s/`, `runtime/`, `screenshots/`, `vendor/`, `tmp_script_prompt.txt`) is **all
> untracked/gitignored** — it is operator-local, not removable via a git PR (a clean `git status`
> confirms git already ignores it). The Obsidian memory vault (`data/memory-vault`, 26 notes, 72
> wikilinks) has **zero real dangling wikilinks** (the only two `[[…]]` misses are literal syntax
> examples inside `CONVENTIONS.md`).

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
| ~~`dashboard/` (root V1 FastAPI + SPA)~~ **→ MOVED TO KEEP** | RESOLVED: the **running** `dashboard` service (`ordo-v2/dashboard-v1:latest`) builds from **root `dashboard/`** (`v2/CUTOVER.md:69` + image history). It is also CI-linted and imported by root `tests/`. See section C. | **KEEP — do NOT delete.** This is the live dashboard build context. |

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

## C. Ambiguities — RESOLVED (2026-07-10 audit) with evidence

Every question below is now answered. Verdict legend: **A** = dead-once-V1-retires (deletable only
in the wholesale V1-retirement PR, together with its CI tests/lint and the root compose that gives it
a build context); **B** = KEEP (load-bearing for the running v2 stack); **B/CI** = KEEP (exercised by
the non-path-gated root CI job regardless of V1's compose).

| Root path | Verdict | Discriminating evidence |
|---|---|---|
| `comfyui-mcp/` | **A + B/CI** | NOT in the live MCP set — `v2/out/mcp/servers.txt` = `memory-vault,qdrant-rag,searxng`; `v2/out/mcp/registry-custom.yaml` defines only those 3. Built ONLY by root `docker-compose.yml` (`build: ./comfyui-mcp`, V1). BUT root `tests/test_comfyui_*` import `comfyui-mcp/managers/workflow_manager.py` and CI ruff-lints `comfyui-mcp` (`ci.yml` pytest job). → dead as a v2 service, but retires **with** its root tests + ci.yml entry, not before. |
| `orchestration-mcp/` | **A + B/CI** | Not in `v2/out/mcp/*`; built only by root `docker-compose.yml` (`build: ./orchestration-mcp`). Ruff-linted by `ci.yml` (pytest job). Same coupling as above. |
| `auth/` (Caddy + oauth2-proxy config, certs, emails) | **KEEP (B)** | De-facto **source of truth** for the edge assets. `v2/plugins/edge/plugin.yaml` bind-mounts `./auth/caddy/Caddyfile`, `./auth/caddy/certs`, `./auth/oauth2-proxy/emails.txt` (relative to `out/`). The render engine does **not** generate these — `out/auth/` was hand-staged from root `auth/` at the 2026-07-09 consolidation (`v2/FLIP.md`) and is **byte-identical** to root `auth/caddy/Caddyfile`. Live `ordo-v2-caddy-1` / `-oauth2-proxy-1` mount the `out/` copy; root `auth/` is the only committed source a re-render/rebuild of `edge` can restore from. **Do not delete.** |
| `config/` (`comfyui-manager-seed.ini`) | **KEEP (B, bootstrap seed)** | One-time bootstrap: `scripts/ensure_dirs.{sh,ps1}` copy `config/comfyui-manager-seed.ini` → `data/comfyui-storage/ComfyUI/user/__manager/config.ini` only if absent. Not mounted by any compose/render, but it is the committed seed for a fresh ComfyUI bring-up. Low-risk to retire eventually, but it is the sole source of that seed — leave it. |
| `tests/` (root V1 pytest suite) | **KEEP (B/CI)** | **Actively exercised by CI** — the `pytest` job (non-path-gated, runs on every push/PR to `main`) does `pip install -r tests/requirements.txt` + `pytest tests/`. It imports `ops-controller/main.py` (`conftest.py`), `comfyui-mcp/…`, `dashboard/…`, and asserts on root `docker-compose.yml`. Retires only as part of wholesale V1 retirement. |
| `scripts/` | **KEEP (B, mixed)** | Load-bearing at runtime: `scripts/llamacpp/` is bind-mounted into the **live** `ordo-v2-llamacpp-1` (`${BASE_PATH}/scripts/llamacpp:/llamacpp-scripts:ro`). Also holds `storage_purge.py` + `stack_monitor.py` (operator crons), `secrets/decrypt.sh` (SOPS), `detect_hardware.py`. Ruff-linted by CI. **Do not delete the directory**; individual V1-only scripts (`compose` wrapper deps, `ensure_dirs`, pullers) can be pruned later, script-by-script, with caller audits. |
| `mcp/` | **A (V1-only)** | Root `mcp/` (Dockerfile + `gateway/{healthcheck,gateway-wrapper}.sh` + `registry.json.example`) is the V1 mcp-gateway build context (`docker-compose.yml: build: ./mcp`). v2 builds its gateway from `v2/docker/mcp-gateway/` (its own diverged `gateway/` with `MEMORY_VAULT_PATH` support), and the live `out/mcp/` config is **render-generated**, not copied from root `mcp/`. So root `mcp/` is unused by v2 — but it is not CI-linted and has no coupled tests, so it is the *cleanest* A candidate; it still shares the root-compose retirement gate (root compose builds from it). |

### `dashboard/` (was flagged "verify" in section A) — RESOLVED: **KEEP (B/CI + running build context)**
The **running** `dashboard` service is `ordo-v2/dashboard-v1:latest`, and per `v2/CUTOVER.md:69` +
image-history inspection it is built from **root `dashboard/`** (context `C:/dev/ordo-ai-stack/dashboard`,
its own `Dockerfile`). The `v2/dashboards/v1-parity/dashboard.yaml` registry entry selects it. Root
`dashboard/` is therefore the production build context **and** CI-linted **and** imported by root
`tests/`. **Do not delete.** (The separate `v2/dashboard/` + `v2/docker/dashboard.Dockerfile` build a
*different*, still-valid, test-covered image `ordo-v2/dashboard:latest` — the selectable `v2-native`
dashboard variant asserted by `v2/tests/test_dashboards.py`; those are **also KEEP**, not dead.)

## C-bis. Dangling doc links found by the audit (report-only)

Fixed in this PR (trivial moved-target):
- `ops-controller/README.md:7` → PRD link pointed at `../docs/Product Requirements Document.md`
  (a single file that was split into `docs/product requirements docs/*.md`); repointed to
  `docs/product requirements docs/index.md`.

Reported, **not** fixed (append-only CHANGELOG history — the targets were external / never-committed
work notes, not paths that "moved to a v2 equivalent"; rewriting historical changelog entries would
add noise/drift). Left as-is; listed so a future editor knows they are known-dead:
- `CHANGELOG.md` L432/434/480/484/486/488/496/507/511/521 — links to `docs/openclaw-*`,
  `docs/architecture/*`, `openclaw/*`, and a couple of `data/comfyui-workflows/*` READMEs that were
  never committed to this repo (OpenClaw-fork / planned-architecture-doc references).

Memory vault (`data/memory-vault`): 26 notes, ~66–72 wikilinks scanned, **0 real dangling** — the two
`[[…]]` misses are literal syntax examples in `CONVENTIONS.md`. Vault untouched (operator content).

## D. Operator-retained soak artifacts (outside the repo — operator deletes, not a repo PR)

From `v2/CUTOVER.md` → "Cleanup candidates …" (these are on-disk, not in git; listed for completeness):

- `C:\dev\ordo-ai-stack\data-v1-snapshot\`, `C:\dev\ordo-v2-data-retired\`, `C:\dev\ordo-v2-out-retired\`
  (the last contains the old `secrets.env` — **shred**, don't just recycle).
- **V1 volume/image prune** (after soak, operator runs — irreversible): remove the `ordo-ai-stack_*`
  volumes + `docker image prune -a`, **EXCEPT keep `ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470`**
  (shared with the running v2 llamacpp).

---

## Recommended order for the cleanup PR

Section C is now resolved (above), so the remaining work is the **wholesale V1-retirement PR** — a
single coupled change, gated on a maintainer's "soak is over, retire V1" decision. Do it all together
or not at all (a partial delete breaks CI):

1. **In one PR**, delete the interlocked V1 unit together: root `docker-compose.yml`, `ops-controller/`,
   `model-gateway/`, `comfyui-mcp/`, `orchestration-mcp/`, `mcp/`, `overrides/`, `Makefile`, `compose`,
   `compose.ps1`, `.env.example`, and the **coupled root `tests/` cases** that import them
   (`test_comfyui_*`, the `ops-controller/main.py` importers in `conftest.py` +
   `test_comfyui_restart_harden.py`, `test_compose_smoke.py`, the `dashboard`-only V1 tests).
2. **In the SAME PR**, edit `.github/workflows/ci.yml`: drop the deleted paths from the `pytest` job's
   ruff line and from the `orchestration-stack-e2e` path filter, and delete/retire the
   `compose-validate`, `compose-smoke`, and `orchestration-stack-e2e` jobs (they validate the removed
   root compose). Otherwise CI goes red the moment the root compose/dirs vanish.
3. **KEEP throughout** (load-bearing for the running v2 stack — see sections B & C): `auth/`, `config/`,
   `scripts/` (esp. `scripts/llamacpp/`), `dashboard/`, `hermes/`, `worker/`, `rag-ingestion/`,
   `qdrant-rag-mcp/`, `codebase-memory-ui/`, `codebase-memory-mcp/`.
4. Gate the PR on: the v2 suite + ruff green, `docker compose -f v2/out/docker-compose.yml config`
   exit 0, and a re-confirmed **0-`ordo-v2`-mounts** proof (nothing deleted appears in any live
   container mount or the rendered compose). Do NOT recreate/restart any live service.
5. Then update `README.md` + `docs/` to drop the now-obsolete V1 pointers.
6. Leave **section D** to the operator (on-disk, post-soak).

> **Why this audit deleted nothing:** no section-A path is a no-op deletion — each is either imported by
> the CI-run root `tests/`, ruff-linted by CI, or the sole build context behind the intentional V1
> rollback asset (`docker-compose.yml`). Removing them piecemeal reddens CI or dismantles rollback one
> day into soak. That is a behavior-changing refactor for a maintainer to trigger, not a safe-tier purge.
