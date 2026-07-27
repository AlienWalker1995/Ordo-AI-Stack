# Ordo — the config render substrate

This repo **is** Ordo: the config render engine and the stack it produces, running in
production. One declarative source (`ordo.yaml`) renders into `./out/`, and services run from that
rendered output — edits to *derived* config never survive a re-render, so drift is structurally
impossible.

The stack runs entirely from `C:\dev\ordo-ai-stack` (`main` is the production branch) as compose
project **`ordo`** (25 services, verified by a fresh `ordo render` with this operator's full plugin
set — see `out/docker-compose.yml`) — containers `ordo-*`, images `ordo/*`, network `ordo-net`. The
render substrate lives at the repo root (there is no `v2/` directory — there is one Ordo).

## Install & first run

**From a fresh machine — one command installs the CLI and launches the setup wizard, in the same
terminal. Run the line for your platform:**

```bash
# macOS / Linux (bash · zsh · or Git Bash on Windows)
curl -fsSL https://raw.githubusercontent.com/AlienWalker1995/Ordo-AI-Stack/main/install.sh | sh
# …or without curl:
wget -qO- https://raw.githubusercontent.com/AlienWalker1995/Ordo-AI-Stack/main/install.sh | sh
```

```powershell
# Windows — PowerShell (not cmd; `curl | sh` needs a POSIX shell, so native Windows uses this)
irm https://raw.githubusercontent.com/AlienWalker1995/Ordo-AI-Stack/main/install.ps1 | iex
```

Either path checks prerequisites (git, Docker + `docker compose` v2, Python 3.11+; warns if there's
no NVIDIA GPU), clones the repo (`~/ordo`, or `%USERPROFILE%\ordo` on Windows — override with the
`ORDO_DIR` env var), installs `ordo` into a virtualenv, and runs **`ordo init`** — the interactive
wizard that configures the whole stack.

**The wizard (`ordo init`) is the setup path.** Every prompt has a sensible default (press **Enter**
to accept); **Ctrl-C** cancels at any point and nothing is written until you confirm at the end. It
walks you through:

1. **Hardware** — confirm the auto-detected GPU / RAM / CPU (or pin it later for reproducibility).
2. **Model** — accept the best-fit catalog pick, or choose another by tier.
3. **Capabilities** — optional groups to enable (chat is always on): image/video, RAG, voice,
   automation (n8n), web search, monitoring, notes sync (cross-device Obsidian /
   [CouchDB LiveSync](runbooks/notes-sync.md)). Default is hardware-gated auto.
4. **Secure front door** — set up the Tailscale + Google SSO gate now, or skip it (with an explicit
   warning that the stack then runs unauthenticated). When you set it up, the tailnet hostname
   (`CADDY_TAILNET_HOSTNAME`), OAuth client id/secret, and email allowlist are **required** — a blank
   answer prompts to defer-or-retry rather than silently shipping a broken gate. Prints the exact
   Google console URL + callback and offers to provision a `tailscale cert`.
5. **External tokens** — Hugging Face, Tailscale, GitHub; all optional (Enter to skip). Internal
   keys (LiteLLM, ops, MCP, cookie, SearXNG, n8n) are auto-generated.
6. **Review & confirm** — a summary of every choice with a final **Y/n**; decline and nothing is
   written.

On confirm it writes `out/ordo.yaml` + `out/secrets.env` (chmod 600, never committed), then
**offers** to render, download the model, and bring the stack up — printing your dashboard URL.
Nothing starts unless you say yes. Re-run `ordo init` any time to reconfigure.

**Manual / already-cloned path** — the wizard just automates this; you can drive the engine directly:

```bash
ordo init                                     # re-run the wizard in an existing checkout
# …or step through it by hand:
ordo --source out/ordo.yaml render --out out  # regenerate out/ from the source (NEVER bare `ordo render`)
ordo preflight --ref out/.env                 # read-only GO/NO-GO readiness gate
# bring up — COMPOSE_PROFILES must list the capability profiles you enabled (the wizard sets these for you):
cd out && COMPOSE_PROFILES=edge,webui,… docker compose -p ordo --env-file .env --env-file secrets.env up -d
```

Everything below is the reference for *how* that render engine works and *why* it's built this way.

## Why this exists (from the architecture interrogation)

Nearly every failure of the current stack traced to **config drift**: the LLM context size,
the model choice, and Hermes' `context_length` were hand-set in three places and fell out of
sync (256K in Hermes vs 128K in llama.cpp → a compaction deadlock; a stale model registry vs
`.env`; etc.). The agreed cure is a **declarative source → regenerated config** model:

> One human-editable declarative source (`ordo.yaml`). Everything derived (`.env`, Hermes
> context, model-gateway ctx, compose vars) is **regenerated** from it. Edits to *derived*
> outputs don't survive a re-render — so drift is structurally impossible. An explicit
> `overrides:` block in the source is the escape hatch that *does* survive.

This is the substrate everything else (scheduler, plugins, installer) renders through, and it's the
direct fix for the #1 pain — now proven in production, not just in test.

## What's here

| File | Role |
|---|---|
| `ordo.example.yaml` | the declarative source — the single source of truth (hardware, tier, model, plugins, overrides) |
| `catalog/models.yaml` | curated model catalog: each entry has resource requirements **and a sha256** (checksums are mandatory — corrupt weights burned us once) |
| `ordo/hardware.py` | hardware detection (GPU/VRAM/RAM/CPU) + mockable profiles for CI |
| `ordo/catalog.py` | load catalog + **best-fit** model selection with a VRAM headroom reserve (encodes the "don't fill the card" lesson) |
| `ordo/config.py` | load/validate the declarative source |
| `ordo/render.py` | `(source + hardware + catalog + plugins) → RenderedConfig`; writes `out/.env`, `out/hermes.context.json`, `out/manifest.json` |
| `ordo/plugins.py` + `services/*/plugin.yaml` | **registry-driven** plugins: each manifest declares hardware needs + a config fragment; the renderer enables what fits (media = NVIDIA-only) and resolves `depends_on` |
| `ordo/scheduler.py` | GPU **scheduler decision engine** — FIFO admission + co-run-when-it-fits + LRU idle-evict (replaces the reactive guardian that caused the outage; the process broker drives it against the real `ordo-` containers — live in production) |
| `ordo/cli.py` | `ordo detect | render | doctor | serve | preflight | …` — the one-script control surface |
| `tests/substrate/` | mocked-profile render (5090 + CPU-only), drift-revert, ctx consistency, plugin gating/deps, scheduler co-run/FIFO/evict, and per-defect-class regression guards from the parity audits (current suite: **172 passed, 2 skipped** — run below) |

## Build history (archival engineering record)
Ordo's render substrate was built slice-by-slice (originally beside the previous stack, on branch
`arch/v2-substrate`), each slice validated before the next. **This section is the historical build
log** — the "V1"/"V2" references below are that history (the previous stack vs this one), not a
current split: today there is only Ordo.

1. **Config render engine** — declarative source → drift-proof config + hardware right-sizing + checksummed catalog. ✅
2. **Plugin registry** — data-only manifests, hardware-gated, dependency-resolved. ✅
3. **Scheduler decision engine** — FIFO + co-run-if-fits + LRU idle-evict. ✅ (the process broker that drives it against the real `ordo-` containers landed in slice 9 and now runs in production as the `ops-controller` service — this is the arbiter that replaced the outage-causing reactive guardian.)
4. **Guided-setup wizard** — `ordo setup` detects → proposes → writes `ordo.yaml` (headless path = CI). ✅
5. **Full-stack parity render + `ordo parity`** — the renderer now reproduces the complete llama.cpp surface (model/ctx/mmproj/MTP args/…), and `ordo parity --ref <.env>` diffs it. ✅
   **Merge-gate (a) demonstrated live:** `ordo parity` vs the real running `.env` → **PARITY OK** (15 keys, 0 mismatches), read-only — proving the engine regenerates today's hand-tuned config from one source with no drift.
6. **Scheduler status API + `ordo doctor` support bundle** — `Scheduler.status()` emits the busy/idle + free-VRAM + running/queued + ETA JSON the dashboard/agents poll; `ordo doctor [--bundle]` exports a secret-redacted diagnostics bundle. ✅ Demonstrated: a 17GB render job + a 4GB chat **co-run** (chat slips beside the render) — the exact eviction-deadlock that broke primus, gone.
7. **MCP as `kind=mcp` plugins** — an MCP server is a manifest (pinned image + env + tools); the renderer composes enabled ones into `out/mcp-registry.yaml` (drift-free) and flags un-pinned images. Runs on CPU. ✅
8. **Compose rendering** — `ordo render` emits an **isolated, runnable** `docker-compose.yml` (own project/network, no host-port clashes, GPU-gated, profile-gated plugins). ✅ The rendered compose is validated by the **real `docker compose config`** engine (both CPU-core and GPU+media shapes), and that check is a CI gate — not just a well-shaped Python dict.
9. **Process broker** — turns scheduler decisions into real container start/stop; the Docker backend is **hard-scoped to the `ordo-` prefix so it can never touch the live stack**. ✅
10. **Control-plane service (`ordo serve` = the `ops-controller` image)** — the substrate over HTTP: `GET /status` (live GPU/scheduler + manifest), `GET/POST /model-config` (drift-safe model switch), `POST /jobs[/complete]` (drive the broker). A real `services/ops-controller/Dockerfile` (built + smoke-tested) makes the compose ref concrete. ✅
    **Validated live in a container:** switching the model over HTTP rewrote `ordo.yaml` **and** regenerated `.env` in one pass (`LLAMACPP_MODEL` + `LLAMACPP_CTX_SIZE` moved together — the drift bug is structurally impossible); unknown model → 404, source untouched. The socket it mounts to drive the broker is guard-scoped to `ordo-*`, so it still can't touch the live stack.
11. **`ordo preflight` GO/NO-GO gate + cutover runbook** — a read-only readiness check for the migration: ctx consistency (drift gate), model/MCP checksums, GPU-present-for-enabled-plugins, **parity vs the live `.env`**, and image readiness (project images blocking, upstream pull-able). Blocking failure → non-zero exit. The runbook is the operator's atomic-cutover procedure (build → preflight → up-beside → validate parity + restore personal backup → flip → rollback-ready). ✅
    **Validated live:** `ordo preflight --ref <live .env>` → **GO**, `parity vs live .env: 15 keys, 0 mismatch`; the unpinned 27b sha256 correctly surfaced as a non-blocking warning.
12. **Dashboard (control plane)** — *a minimal V2-native SPA was built here first, but it was a regression: it dropped the operator's feature-rich V1 dashboard (GGUF mgmt, model-control flag cards, GPU/model-registry views, Grafana tab, token auth).* **In production the ORIGINAL V1-parity dashboard is reinstated** — service `dashboard` runs image `ordo/dashboard-v1` (the V1 SPA reused unchanged) against a NEW backend service **`ops-api`** (a copy of V1's ops-controller with guardian/watchdogs OFF and per-service recreate on). Dashboard selection is now data-driven (`services/<id>/dashboard.yaml`, mirrors the agent registry): `native` (renamed from `v2-native`) stays the open-source default, this deployment pins `dashboard: v1-parity`. Every tab/widget was validated feature-by-feature. Note: the `ordo serve` scheduler control plane stays named `ops-controller` (its live clients depend on that name); `ops-api` is the separate dashboard backend.

13. **One-command packaging + mocked-profile CI** — `pyproject.toml` installs the substrate as a real `ordo` command (`pip install .`; runtime dep = just PyYAML, so the core runs anywhere); `python -m ordo` also works. A dedicated **`substrate` CI job** (in `.github/workflows/ci.yml`, path-gated on `ordo/**`, `services/**`, etc., pinned deps) runs ruff + the full mocked-profile suite + a fresh-install render smoke — the merge-gate "mocked-profile CI" + "clean fresh-install" requirements. ✅
    **Validated:** simulated the CI on a `python:3.12` runner-equivalent — ruff clean, 67 tests, `python -m ordo render` from a clean checkout, and `pip install` → a working `ordo detect`.
14. **Multi-agent adapter contract (Hermes default, pluggable)** — an agent is a data manifest (`services/<id>/agent.yaml`) declaring its image + the core services it consumes; `ordo/agents.py` resolves the chosen agent, and `render` wires its image into the compose `agent` service. Hermes is `default: true`; a pinned `openai-agent` reference adapter proves the core is genuinely agent-agnostic; an unknown agent is warned at render/preflight (convention fallback) not silently broken at `compose up`. The contract (chat via model-gateway, tools via mcp-gateway, GPU via ops-controller `/jobs`, `.env` read-only) is documented in [`agents.md`](agents.md). ✅
15. **Native (non-Docker) path** — `ordo native` builds the exact `llama-server` argv from the *same* rendered `LLAMACPP_*` env the container uses (model/ctx/gpu-layers/kv-type/rope/mmproj/MTP extra-args), proving the source is deployment-mode-agnostic — Docker or bare process, one source, no divergence. Best-effort by design: it's honest about the pieces native mode doesn't orchestrate (gateways/agent = manual steps; media/voice = Docker-only). ✅
16. **Cloud fallback + a starvation-bug fix** — building this surfaced a real latent bug: `pump()` used to `break` on a job too big for the GPU, permanently stalling every smaller job queued behind it. Fixed: a can-never-fit job is removed from the queue — **routed to cloud** when `cloud_fallback.enabled`, else **rejected** — and pumping continues, so small jobs never starve. `status()` surfaces `cloud_routed`/`rejected`; routed jobs are queryable-and-drained via `GET /jobs/cloud-routed` (each job is handed out exactly once, to whichever agent polls it) rather than auto-dispatched; the broker never starts a routed job locally. ✅
17. **`ordo fetch` — offline model provisioning with mandatory checksum** — downloads catalog models and **refuses to trust unpinned or corrupt weights**: a null-sha256 entry is refused for download unless `--allow-unverified`, a post-download hash mismatch deletes the file and errors (never leave corrupt weights to load into noise), and an already-verified file short-circuits with no network call — so once fetched, installs are **offline-capable**. Hashing/planning/verify-reject logic is pure + fully tested (download injected); only the network shells out. ✅ Demonstrated: `fetch --all --plan-only` refuses the 4 unpinned Qwen entries and cleanly plans the pinned 27b.
18. **Data-driven plugin services + `monitoring` & real `voice` bundles (V1 PR #71 + #45 parity)** — the plugin schema now declares its compose services as **data** (`services: [{name, image, gpu, gpu_pin, env, command, volumes, healthcheck, depends_on}]`); `compose.py` builds them from the resolved manifests instead of hardcoded if-blocks (comfyui/song-gen/voice migrated). Two bundles land as first-class plugins: **`monitoring`** (Grafana + Prometheus + `nvidia_gpu_exporter`, all sha-pinned; CPU-ok so it runs anywhere; keeps the driver-581.80 `--query-field-names` crash-fix; `render` now emits `--metrics` on `llama-server` so Prometheus can scrape `:8080`; named volumes are declared at the compose top level), and the **real `voice`** (faster-whisper **stt** + Kokoro **tts**, sha-pinned). Voice introduces **`gpu_pin: secondary`**: because those images have no Blackwell kernels and CRASH on the 5090, `hardware.detect()` now captures each GPU's uuid and render pins them to the **non-primary (Pascal 1070)** card via `CUDA_VISIBLE_DEVICES` + a `device_ids` reservation (the only pin WSL2 honors); with no secondary GPU the plugin is **gated OFF with a warning** rather than shipping a guaranteed crash. ✅ Validated: mocked dual-GPU (5090+1070) enables voice pinned to `GPU-20fac13a-…`, single-5090 disables it with a warning, CPU-only disables it; the rendered dual-GPU compose (profiles `media`+`voice`+`monitoring`) passes the real `docker compose config`. Live stack untouched.

19. **Full V1→V2 service parity + secrets model (this slice)** — the substrate now reaches
    **service-level parity** with the live stack. Every V1 `docker-compose.yml` service is accounted
    for: 12 already-covered (core/existing plugins), **9 ported now** as new
    `kind=service` plugin manifests — **`rag`** (qdrant + llamacpp-embed + rag-ingestion),
    **`worker`**, **`automation`** (n8n), **`open-webui`**, **`searxng-web`**,
    **`codebase-memory-ui`**, **`hermes-dashboard`**, and the opt-in **`edge`** (Caddy + oauth2-proxy,
    the *only* host-port publish, profile `edge`) — and 4 obsolete-by-design (model pullers → `ordo
    fetch`; the manager-setup shim → image build; the reactive guardian → the V2 scheduler). Each
    ported service preserves V1's **exact image pins** (qdrant `v1.18.2`, n8n `2.28.3`, open-webui
    `v0.10.1`), floating `:latest` tags are **digest-pinned** (searxng), and env keys / volumes
    (bind + named) / healthchecks / profiles / depends_on carry over verbatim.
    **Image parity fixed:** `model-gateway` + `mcp-gateway` now reference V1's custom config-wrapper
    builds as **project buildable images** (`ordo/model-gateway:latest`, `ordo/mcp-gateway:latest`
    — contexts under `services/`) instead of the unconfigured upstream `litellm:main` / `mcp-gateway`,
    so the `local-chat` alias + reload wrapper survive and `preflight` reports "build first". The two
    MCP **placeholder digests** are replaced with real refs (qdrant-rag = a project buildable image,
    searxng = the live registry digest). **Secrets model:** derived `.env` and operator secrets stay
    in **separate files** — services that need secrets read a second env_file `secrets.env`
    (`required: false`, so a missing one never fails `docker compose config`), and `ordo render`
    emits **`secrets.env.example`** listing the required KEYS (names only, values empty) gathered from
    the core set + each enabled plugin's `secrets:`. `ordo preflight --secrets <file>` adds a
    non-blocking check for missing keys. ✅
    **Validated:** the full dual-GPU render enables all 12 service plugins + 2 MCP with **zero
    warnings**; the rendered compose with **all 10 profiles** passes the real `docker compose config`
    (27 entries, caddy the sole host-port publisher, CADDY_BIND `:?` failsafe preserved); the CPU-only
    render validates too; `ordo preflight` → GO, MCP "all pinned".

20. **Memory vault (file-based MCP)** — a shared markdown vault
    (`data/memory-vault`, seeded with `README`/`CONVENTIONS` + the `name`/`description`/`type`
    frontmatter schema) is the durable agent-memory substrate: plain `.md` files, no hidden store.
    The **`memory-vault`** plugin ships it (`kind=mcp`, `@bitbonsai/mcpvault` version-pinned in
    `services/memory-vault`, 15 tools: read/write/patch/search/frontmatter/tags/…). The vault is browsed
    with **native Obsidian on the operator's machine**, opened at `data/memory-vault` — the SAME host
    dir the MCP mounts, so agent writes appear in the desktop app and vice-versa. To let a
    file-based MCP **write** its data dir, the render engine now passes the upstream gateway-catalog
    fields through — `volumes` (a **read-write** host bind, substituted from `MEMORY_VAULT_PATH` by
    the gateway wrapper), `command`, `longLived`, `disableNetwork` — which the previous image+env-only
    MCP render dropped; existing MCP entries render byte-identically (passthrough is opt-in). ✅
    Validated live: `write_note` through the gateway persists a real file on disk that native Obsidian
    sees; `read_note`/`search_notes` round-trip; llamacpp/agent untouched.

`ordo render` writes the complete stack (`.env` + `docker-compose.yml` + `hermes.context.json` +
`manifest.json` + `mcp-registry.yaml` + `secrets.env.example`); `ordo serve` runs the control plane
(service `ops-controller`) that regenerates it drift-safely at runtime; `ordo preflight` gated the
cutover. **Test suite: 181 passed, 2 skipped** (verified 2026-07-09).

## Operating this stack (it IS production now)
The 24 services run under compose project `ordo` from `C:\dev\ordo-ai-stack`, all reached through
the edge — Caddy is still the **only** service that publishes host ports, but since 2026-07-24 it
listens on **seven** SSO-gated ports on `${CADDY_TAILNET_HOSTNAME}`, one per UI surface, instead of
mounting every app under a subpath of a single `:443`:

| Port | Service |
|---|---|
| `:443` | front door — landing page, `/oauth2` (the one Google callback), `/llm/*` (LiteLLM API, Bearer), `/mcp` (Bearer), n8n webhook/OAuth passthroughs (`/n8n/webhook/*`, `/n8n/rest/oauth2-credential/callback` — external URLs unchanged), and 302s from every legacy subpath |
| `:8443` | Open WebUI (chat) |
| `:8444` | Dashboard (+ `/grafana/` embed) |
| `:8445` | n8n UI |
| `:8446` | ComfyUI |
| `:8447` | Hermes (served at its own port root) |
| `:8448` | codebase-memory (served at its own port root) |

One Google sign-in covers all seven ports **and the clean per-service tailnet names** — the
oauth2-proxy cookie is domain-scoped and the SSO gate's `rd=` carries `{host}` (portless), so a
single wildcard `--whitelist-domain=.<domain>` covers every port and sidecar name at once; the Google
OAuth client needs no new redirect URIs. Old subpath URLs (`/chat`, `/dash`, `/n8n`, `/comfy`,
`/hermes`, `/codebase-memory`, `/grafana`) 302 from `:443` to their new ports, so bookmarks keep
working, and n8n's public webhook base (`N8N_WEBHOOK_URL=https://<host>/n8n`) is unchanged. This
retires the subpath-rewrite class of workaround (Open WebUI root-catchall, Hermes header-based
base injection, n8n `strip_prefix`, codebase-memory nginx rewrites) in favor of giving every
prebuilt SPA the root it was actually compiled for. One data root at
`C:\dev\ordo-ai-stack\data` (Hermes brain at `data\hermes`). Secrets live in gitignored
`out\secrets.env` (a second `env_file`).

This Tailscale front door is the **default of three swappable access layers** — the edge
(`services/edge`) and clean-URL names (`services/tailnet-names`) are plugins, so the same rendered stack
can instead sit behind a self-hosted public domain or a cloud VM (both keeping the same Google SSO
gate). Only the Tailscale model is wired today; the others' required pieces are documented in
[`deployment-models.md`](deployment-models.md).

**Render discipline** (the drift cure, in daily operation):
- Change config by editing the source `ordo.yaml`, then **re-render** — never hand-edit `out/.env`.
- Always render from the real source: `ordo render --source out/ordo.yaml`.
- **Re-render only inside a `--gpus all` container** (so hardware detection sees both cards); the
  rendered `llamacpp` block must come out **byte-identical** to what's running.
- Apply with `docker compose ... up -d --no-deps <svc>` (per-service, no cascade). The dashboard's
  per-service recreate button does exactly this against the existing `out/` compose (no re-render).

## What the cutover produced
The 2026-07-09 cutover took this substrate to production: 3 flip attempts (2 clean ~7-min rollbacks
that each converted a live defect into a test-guarded fix; success at ~3.75-min core downtime),
then a consolidation that re-homed everything to `C:\dev\ordo-ai-stack` and merged to `main`. The
operator-specific images (`agent-hermes` wrapping the Hermes `data/`, `comfyui`, `voice`) are built
and running; the dashboard reinstatement is `ops-api` + `dashboard-v1` (above). The 27b model is
**sha256-pinned** (`c03727f9…`, computed from the on-disk weights) so `preflight`'s checksum gate
stays green.

## Design acceptance gates (all met, proven in production)
1. Renders a full config from one source with **zero hand-edits**.
2. **Drift-revert**: a hand-edited derived value is corrected on the next render.
3. Renders both a **5090 profile and a mocked CPU-only profile** into valid configs.
4. **Consistency**: the one ctx value is identical across `.env`, Hermes, and model-gateway
   (the exact bug that started this).

## Run the tests (no host Python needed)
```
docker run --rm -v "$PWD:/w" -w /w python:3.11-slim \
  sh -c "pip install -q -r requirements-dev.txt && PYTHONPATH=. python -m pytest tests/substrate -q"
```
