# Changelog

All notable changes to this project are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Removed
- **Media worker service retired (2026-07-28).** The headless dashboard "media worker"
  (`services/worker/` plugin, container `worker`, image `ordo/worker`) — a durable SQLite-queue
  job processor that drove ComfyUI render + publish jobs and cron-style schedules — is gone. It
  had processed nothing since 2026-06-14 (0 schedules, 0 outbox rows): the live reel pipeline is
  driven entirely by **Hermes cron + the direct `render_publish_dialogue.py` script** (ComfyUI +
  an `ops-controller` GPU lease + an n8n webhook), never by this worker. Removed: the plugin
  manifest/Dockerfile/`worker.py` under `services/worker/`, its compose service and `WORKER_*`
  env vars (`WORKER_ENABLED`, `WORKER_POLL_INTERVAL_SEC`, `WORKER_CONCURRENCY`), its dashboard
  health card ("Media Worker"), and its `ops-api` `ALLOWED_SERVICES` / dashboard
  `OPS_SERVICE_MAP` entries. The dashboard Orchestration tab drops its "Recent jobs" panel. The
  worker-backed `/api/orchestration` endpoints (`/run`, `/jobs*`, `/publish/*`, `/schedules*`)
  are retired; the worker-**independent** endpoints stay live — `/readiness`, `/workflows*`,
  `/validate`, `/outputs`, `/comfyui/status`, `/comfyui/restart`, `/registry/*`, `/gpu*` — as
  does the orchestration MCP server's `comfyui_status`/`workflows`/`outputs`/`registry`/`gpu`
  tool surface (its job/schedule/publish tools are dropped with the worker).
- **ai-toolkit plugin retired (2026-07-24, `2cc34e8`).** The `ai-toolkit` service plugin (below)
  is gone from `plugins/`; LTX-trainer is promoted to sole LoRA trainer following the ai-toolkit
  vs. LTX-trainer bake-off. The `:8443` SSO UI and the ai-toolkit-specific GPU lease wiring are
  removed; LTX-trainer uses the same shared `lease-exec` seam.

### Added
- **ai-toolkit plugin**: ostris/ai-toolkit LoRA trainer as a core V2 service. Training runs
  acquire an exclusive primary-GPU lease via ops-controller (new generic `POST /jobs/heartbeat`
  renewable leases + `v2/assets/lease-exec.py` wrapper mounted at the UI's venv-python spawn
  seam). UI at `https://<host>:8443/` (own Caddy site — the prebuilt Next.js app can't serve
  under a subpath; SSO-gated like everything else).

### Changed
- **v2 substrate rebuild + 2026-07-09 cutover — `main` is now production.** The stack was rebuilt on
  a declarative **render-engine substrate** (`v2/`): one source (`v2/ordo.yaml`) regenerates `.env`,
  compose, Hermes context, and the MCP registry into `v2/out/`, so config **drift is structurally
  impossible** (the class of failure — 256K-vs-128K ctx, stale model registry — that motivated the
  rebuild). The **reactive VRAM guardian was removed** and replaced by the proactive `ordo serve`
  **scheduler** (FIFO admission + co-run-when-it-fits + LRU idle-evict), designing out the
  eviction-deadlock outage that triggered the rebuild. GPU work is requested through the
  ops-controller (`POST /jobs`); patched llama.cpp runs on the primary GPU (5090), voice on the
  secondary (1070). The V1-parity dashboard control plane was reinstated on the v2 stack (backend
  service `ops-api`). Agents became **data manifests** under `v2/agents/` with **Hermes as the
  default** agent. Full V1→V2 service map: `v2/PARITY.md`.
  **Cutover (2026-07-09):** an atomic big-bang flip (v2 brought up beside the live stack, validated
  for parity, then flipped) executed after 3 attempts, with **≈3.75 min core chat-path downtime**;
  then **consolidated to a single primary checkout** (`C:\dev\ordo-ai-stack`, one data root at
  `…\data`) with the `C:\dev\ordo-v2` worktree retired. Merged to `main` via **PR #72** (merge commit
  `d115035`). The old V1 stack was kept intact as a rollback asset (containers removed, volumes +
  images retained). See `v2/CUTOVER.md` + `v2/FLIP.md`. The **top-level V1 layout** (root
  `docker-compose.yml`, root `ops-controller/`, root `model-gateway/`, `overrides/`, the guardian) is
  now **LEGACY**, pending a separate deliberate cleanup PR.

### Added
- **Codebase-Memory MCP — opt-in `--profile codebase-memory` code knowledge graph for Hermes.**
  Adds `codebase-memory`, a gateway-spawned stdio MCP server wrapping the upstream
  `DeusData/codebase-memory-mcp` static binary (MIT; bundled offline embeddings, no API
  keys). Gives Hermes structural code navigation (`search_graph`, `trace_path`,
  `get_architecture`, `get_code_snippet`, ...) over the repos under `CODE_ROOT`, mounted
  read-only at `/c/dev`. The SQLite index persists in the `codebase-memory-cache` named
  volume; the spawned container is `longLived` (warm across calls) and `disableNetwork`
  (no egress). Image is checksum-pinned (portable/static build, v0.8.1). Enable with
  `docker compose --profile codebase-memory build codebase-memory-mcp-image` then
  `./scripts/mcp_add.sh codebase-memory` (set `CODE_ROOT` first). Indexing honors
  `.gitignore` + a new root `.cbmignore` (defense-in-depth secret/non-source excludes).
  Also adds an optional **`codebase-memory-ui`** service — the upstream interactive 3D
  graph **visualization** — served at `https://<host>/codebase-memory/` on the shared
  `:443` Google-SSO origin and listed in the dashboard services section. The UI is an
  absolute-asset SPA with no base-path option (its `/assets`/`/api` collide with Open
  WebUI's root), so the image runs nginx that proxies the localhost-only UI and
  `sub_filter`-rewrites its baked paths to the `/codebase-memory/` prefix. It indexes
  the code root (`/c/dev:ro`) in its own process (in-memory; re-index after a restart).
- **Voice STT/TTS services — opt-in `--profile voice` with secondary-GPU pinning.**
  Adds two OpenAI-compatible local speech services: `stt` (`fedirz/faster-whisper-server:latest-cuda`,
  `/v1/audio/transcriptions` at `http://stt:8000/v1`) and `tts` (`ghcr.io/remsky/kokoro-fastapi-gpu:latest`,
  `/v1/audio/speech` at `http://tts:8880/v1`, default voice `af_bella`). Both services are
  internal-only (no host ports, `backend` network), use `profiles: ["voice"]`, and default to
  `STT_COMPUTE_TYPE=int8` for Pascal compatibility. `detect_hardware.py` assigns `stt`/`tts` to
  the secondary GPU (`gpus[-1]`) when more than one GPU is detected, falling back to the primary
  on single-GPU hosts — keeps the primary GPU free for the LLM. The model registry seeds
  `voice-stt` (kind `stt`, `est_vram_gb=2.0`) and `voice-tts` (kind `tts`, `est_vram_gb=1.0`)
  on first run from `STT_MODEL` / `TTS_VOICE` env vars. Both services are in `ALLOWED_SERVICES`
  and `GPU_ASSIGNABLE_SERVICES`. Enable with `docker compose --profile voice up -d`. HF weights
  cached at `${DATA_PATH}/voice/hf-cache`. Images are sha-pinned; NOTE both must run on a
  Pascal-class GPU (no Blackwell kernels), which the registry's secondary-GPU pin handles.
  Hermes wiring: STT (voice memo → text) is fully local via `STT_OPENAI_BASE_URL` set on
  `hermes-gateway`; TTS voice replies use Hermes' default `edge` provider — the current Hermes
  config schema doesn't persist a TTS `base_url`, so local Kokoro isn't Hermes-wired for replies
  (the service is provided for direct/n8n/future use). See `docs/configuration.md`.

- **Model registry — single source of truth for model↔GPU assignment.** `ops-controller/model_registry.py` introduces `ModelRecord` + `ModelRegistry` backed by `data/model-registry.json`. The registry is the *intent layer*: it tracks which model file runs on which GPU UUID, estimated VRAM, enable/disable state, and per-service config (ctx size, mmproj, KV-cache types). `.env` and `overrides/gpu-assignments.yml` are now *derived enforcement* — both are written from registry state, never the other way around. On startup ops-controller reconciles the registry from those files (seed-only: existing records are preserved). REST endpoints `/registry/{models,gpus}` expose full CRUD + GPU-assign + enable/swap; Hermes and the dashboard are equal clients. The legacy `POST /gpu/assign` path keeps the registry in sync after a successful assign so no client ever sees a stale pin. A dependency-free shared formatter module (`ops-controller/gpu_assignments_fmt.py`) ensures `detect_hardware.py`, `model_registry.py`, and `main.py` all emit identical gpu-assignments YAML (single-quoted `device_ids`; double-quoted legacy files are still parsed). `MODEL_REGISTRY_PATH` (default `/data/model-registry.json`) controls the backing store location. Fine-grained GPU pinning for single-model services (`llamacpp`, `llamacpp-embed`); coarse for `comfyui` (multi-model runtime).

- **Playwright registered as a stack-managed MCP server (default-on).** `mcp/gateway/registry-custom.yaml` now pins `playwright` (sha-pinned `mcp/playwright` image) and it is included in the default `MCP_GATEWAY_SERVERS` (`gateway-wrapper.sh`, `docker-compose.yml`, `.env.example`). Previously `playwright` was listed in `data/mcp/servers.txt` but had no local catalog entry, so the gateway resolved it from Docker's **unpinned online catalog** — non-reproducible and leaking orphan `mcp/playwright` containers across gateway reloads. Pinning it here makes the stack own the definition; Hermes keeps its `browser_*` tools. Note: this image exposes `browser_run_code_unsafe` (RCE-equivalent) — acceptable for the single trusted operator, restrict with `--caps` if exposed more widely.

### Removed
- **Retired Tavily scaffolding fully removed.** Tavily was replaced by self-hosted SearXNG on 2026-05-12 and the `secrets/tavily_key.sops` Docker secret was deleted in #38; this removes the remaining re-enable scaffolding so no Tavily API key can be wired back in by accident: the `tavily` block in `mcp/registry.json.example`, the `tavily_key.sops`/`TAVILY_API_KEY` references in `secrets/README.md`, `docs/runbooks/secrets.md`, `docs/configuration.md`, and the security/mcp-gateway PRD docs. The `tvly-` pattern in `scripts/secrets/audit-git-history.sh` is intentionally **kept** as a guard against accidental future key commits. (Upstream Hermes' own built-in Tavily web backend under `vendor/hermes-agent/` is untouched — the stack reaches search via the SearXNG MCP, not that backend.)

### Changed
- **ComfyUI custom-node deps auto-install on container start.** The `comfyui` service's command shim now loops `pip install -r requirements.txt` over each `/root/ComfyUI/custom_nodes/*/` before exec'ing `/runner-scripts/entrypoint.sh`. Previously a manual `pip install` after recreate was required because the deps live on the container's writable layer (e.g. `juno-comfyui-nodes` needs `faster-whisper`, `edge-tts`, `soundfile` for caption rendering — every recreate wiped those). Idempotent: warm-cache restarts skip already-satisfied specifiers; failures on individual `requirements.txt` files (e.g. `ACE-Step-1`) log `[deps] WARN failed` and continue. The pre-existing manual API at `POST /api/comfyui/install-node-requirements` is unchanged.

### Fixed
- **GitHub-monitor cron no longer blocked by invisible unicode.** `scripts/stack_monitor.py` fetches upstream GitHub release names and commit messages, which routinely embed zero-width / invisible "format" (Cf) characters — most often the ZWJ `U+200D` inside emoji sequences like 👨‍💻. When the JSON report was fed back into Hermes to format for Discord, that invisible unicode tripped the prompt-injection scanner and the whole daily cron failed (`Blocked: prompt contains invisible unicode U+200D`), every run. The monitor now strips all Cf-category characters from the entire output (recursively, both `--json` and human-readable) before emitting; visible text and emoji are unaffected (a ZWJ emoji renders as its component glyphs). Covered by `tests/test_stack_monitor_sanitize.py`.
- **ComfyUI restart storm hardened (status verb + server-side debounce).** During a failed render cron, Hermes tried to recover ComfyUI by hammering guessed paths `POST /api/comfyui/restart` and `GET /api/comfyui/status` against both ops-controller (404) and the dashboard (401), ~every 3s for ~2 min — it had no canonical *status* verb so it improvised raw HTTP. Two fixes: (1) ops-controller `POST /services/{id}/restart` now **debounces** rapid repeats per-service (`OPS_RESTART_DEBOUNCE_SECONDS`, default 20) so a retry-loop collapses into one in-flight restart instead of stacking `docker restart` calls; (2) a new ComfyUI-**independent** health verb — dashboard `GET /api/orchestration/comfyui/status` + orchestration-MCP `comfyui_status` tool — that reports container state + render-queue reachability by querying the dashboard→ops control plane (which stays up when ComfyUI is down), so the agent checks status via a real tool instead of guessing. The `restart_comfyui` tool docstring now points agents at `comfyui_status` rather than tight-looping.
- **Dashboard at `/dash/` renders fully behind SSO front door (#14).** Three coupled bugs prevented the dashboard from working when served at `/dash/`: (a) all `fetch('/api/...')` calls used absolute paths and missed Caddy's `/dash/*` prefix → `index.html` now detects `APP_PREFIX` from `location.pathname` and prepends it inside the `api()` wrapper, the bootstrap `/api/auth/config` fetch, and the favicon link; (b) service-card links pointed at direct upstream ports (e.g. `:8188`) that aren't host-published — `serviceOpenHref` now maps known SSO-gated services (`comfyui` → `/comfy/`, `webui` → `/chat/`, `n8n` → `/n8n/`, `hermes` → `/hermes/`) to their Caddy paths when `APP_PREFIX` is set; (c) `/api/auth/config` advertised `auth_type=bearer` even when the request arrived from the trusted reverse proxy with a verified `X-Forwarded-Email` → endpoint now returns `auth_required=false` on the SSO path so the bearer-token modal doesn't pop up.
- **Caddy `route { forward_auth + handle_path }` pattern restored on main.** The original `handle @auth { forward_auth }` shape was terminal — once `forward_auth` returned 202, the request ended without ever reaching the path-prefix handler, so SSO-gated routes (`/dash/`, `/chat/`, `/n8n/`, `/hermes/`, `/comfy/`) returned an empty 202 instead of the upstream UI. Pairs with the `--email-domain=*` removal in oauth2-proxy (the wildcard bypassed the `--authenticated-emails-file` allowlist).
- **Caddy moved off `frontend` network.** Membership on both `proxy-net` and `frontend` made Docker DNS return whichever IP it picked first for upstream services. When it chose the `frontend` IP, dashboard saw caddy as outside `DASHBOARD_TRUSTED_PROXY_NET=172.24.0.0/16`, refused the `X-Forwarded-Email` header, and 401'd every `/api/*` call. Caddy is the SSO ingress only and reaches every upstream over `proxy-net`; dropping `frontend` membership eliminates the ambiguity.
- **ops-controller Phase 1 + Phase 2 restored (#15).** Post-drain `/free` to release ComfyUI's PyTorch caching-allocator pool and VRAM-pressure watchdog re-enabled.

### Changed
- Hermes Agent migrated from host-mode install to Docker compose services (`hermes-gateway` + `hermes-dashboard`). One `docker compose up -d` now brings the whole stack online atomically. Auto-restart, `depends_on: service_healthy` coordination, internal-DNS health probe from the Ordo dashboard (`http://hermes-dashboard:9119/`). Deletes `scripts/start-hermes-host.sh` and the global `hermes` wrapper at `~/.local/bin/`. Operator runtime state at `data/hermes/` is preserved — Docker-network endpoints are re-seeded on each container start by the entrypoint.

### Changed
- **Compute Pressure overhaul:** `COMPUTE PRESSURE` panel now shows CPU%, RAM%, and (where applicable) VRAM% per toolkit service, sorted by current pressure so the hog is always on top. New ops-controller endpoint `/stats/services` merges `docker stats` with NVML per-PID VRAM. Dashboard proxies via `/api/hardware/service-pressure` (no auth, same pattern as `/api/hardware`). On Windows/WSL2 where per-PID VRAM is unavailable, panel falls back to a single aggregate GPU row. Replaces `/api/hardware/gpu-processes` and the PID-labeling heuristic.

### Security

- **Timing-safe token comparison:** Auth token verification in dashboard and ops-controller now uses `hmac.compare_digest()` instead of `==`, preventing timing side-channel attacks.

- **LLAMACPP_EXTRA_ARGS injection prevention:** `POST /env/set` now validates `LLAMACPP_EXTRA_ARGS` values with a strict character allowlist, preventing shell injection via backtick/subshell syntax when the value is word-split in run scripts.

- **Hardened CSP headers:** Added `connect-src 'self'`, `frame-ancestors 'none'`, `base-uri 'self'`, and `form-action 'self'` directives to Content-Security-Policy.

- **XSS fix in service cards:** Service hint text was injected as raw HTML; now uses `escapeHtml()`.

- **SSRF protection on outbox webhooks:** `publish_enqueue` endpoint now validates webhook URLs against private/reserved IP ranges using `ipaddress` module, preventing SSRF via crafted callback URLs.

- **Non-root ops-controller container:** ops-controller Dockerfile now runs as `appuser` (UID 1000) with docker group access instead of root.

- **Failed-auth logging:** Dashboard and ops-controller now log `AUTH_FAIL` warnings with path, method, and source IP on every rejected authentication attempt, enabling brute-force detection.

- **Audit log value masking:** `env_set` audit entries now log `len=N` instead of the first 80 chars of the value, preventing credential leakage if token-type keys are ever added to the env allowlist.

- **TruffleHog CI pinning:** Secret scanning action pinned from mutable `@main` to release tag `@v3.88.26`, preventing supply-chain attacks via compromised upstream commits.

- **ca-certificates preserved in ops-controller:** `apt-mark manual ca-certificates` prevents auto-removal when curl is purged, ensuring Python HTTPS calls work at runtime.

- **Token leak in services catalog:** The agent gateway token was embedded in the URL returned by the unauthenticated `/api/services` endpoint, leaking the auth token to any client. The token is now stripped from the public URL.

- **Path traversal in workflow templates:** `load_template()` accepted template IDs containing `../`, allowing reads outside the templates directory. Now validates the resolved path stays within the templates root.

### Fixed

- **Gemma token bleeding in `run_workflow`:** Gemma 4 leaks turn-separator tokens (`<|"|>`) into tool-call argument strings, producing values like `"mcp-api/generate_video"` (with surrounding quotes). `comfyui-mcp` now strips Gemma special tokens and balanced surrounding quotes from `workflow_id` before path resolution, so the workflow lookup succeeds regardless of token artifacts.

- **`frames` parameter required in video workflows:** `PARAM_INT_FRAMES` was not in the `optional_params` set, so LTX-2.3 video workflows required explicit `frames` instead of defaulting to 121. Added `frames` to optional params; default 121 (5 s at 24 fps) now applies automatically.

- **Missing `generate_video.wfmeta`:** `mcp-api/generate_video` lacked a sidecar metadata file, so the ComfyUI MCP catalog had no descriptions, defaults, or parameter guidance for video generation. Added `generate_video.wfmeta` with LTX-2.3 defaults (576×1024, 121 frames, 24 fps, cfg=3.5, 40 steps).

- **`ltx-2.3-t2v-basic` missing from default model packs:** The default pull list included `gemma-3-text-encoder-fp4` (wrong filename) but not `ltx-2.3-t2v-basic`, which provides the `fpmixed` Gemma encoder, KJ VAE, and text projection files actually referenced by `mcp-api/generate_video`. Updated defaults to `ltx-2.3-fp8` + `ltx-2.3-t2v-basic` + `ltx-2.3-extras`.

- **`ttft_ms` always zero in service-usage:** `/api/throughput/service-usage` was building per-service dicts without `ttft_ms`, so the `last_ttft_ms` stat always reported `0.0`. Now correctly propagated from the raw samples.

- **Worker retry race condition:** Retry job creation used `list_jobs(limit=1)` to find the new job by `retried_from` — with concurrency >1, a different job could occupy the slot, leaving `retry_count=0` and enabling unlimited retries. Now uses `create_job()` return value directly.

- **Dead `:path` route modifier:** `DELETE /api/comfyui/models/{category}/{filename:path}` declared `:path` (allows slashes) but immediately rejected any filename containing `/`. Changed to plain `{filename}`.

- **VACUUM blocks readers under load:** `vacuum_db()` ran with 30s busy_timeout, blocking all dashboard reads during the full rewrite. Now uses 5s timeout and logs a debug message on skip. Worker also skips vacuum when jobs are in-flight.

- **Worker retry crash protection:** If retry logic itself fails (e.g. corrupted compiled_workflow), the failure handler now catches the inner exception and marks the original job as failed instead of crashing the worker thread.

- **Workflow version collision:** `save_workflow_version` and `rollback_workflow` used non-atomic SELECT MAX + INSERT for version numbers, allowing collisions under concurrent saves. Now uses atomic `INSERT…SELECT` with `COALESCE`.

- **Throughput state corruption on crash:** `_save_throughput_state()` wrote directly to `throughput.json`; a crash mid-write produced truncated JSON, losing all historical data on next load. Now uses atomic write-then-rename.

- **Job state machine enforcement:** `update_job` now validates state transitions against a defined state machine, preventing invalid regressions (e.g. `published -> queued`). Invalid transitions are logged and silently ignored.

- **Refresh button stuck in loading state:** `refresh()` did not clear the loading spinner on error; wrapped in try/finally so the button always recovers.

- **loadComfyuiPacks crash on error response:** Missing `r.ok` check caused `.json()` to parse an error body and produce confusing UI. Now returns early on non-OK responses.

- **loadMcpServers crash on error response:** Same missing `r.ok` guard; added early return on non-OK responses.

- **Stale promoted workflow versions:** `promote_workflow_version()` set `promoted_at` on the new version but never cleared it on previously promoted versions, leaving multiple rows with non-NULL `promoted_at`. Now demotes all prior versions before promoting the target.

- **Outbox attempt counter race condition:** `record_outbox_attempt()` used a read-then-write pattern (SELECT attempts, then UPDATE), which could lose increments under concurrent calls. Now uses a single atomic `UPDATE SET attempts = COALESCE(attempts, 0) + 1`.

- **Blocking I/O in comfyui_packs endpoint:** `_scan_comfyui_models()` and `config_path.read_text()` ran synchronously on the async event loop, blocking all other requests during filesystem I/O. Now uses `asyncio.to_thread` and `_read_json_async` respectively.

- **ComfyUI pull subprocess can hang forever:** `_run_comfyui_pull_subprocess` called `proc.wait()` without a timeout; a hung child process would permanently block future pulls (409 guard). Now uses `proc.wait(timeout=7200)` with `proc.kill()` on timeout, and kills the process on unexpected errors to prevent resource leaks.

- **Dashboard missing ComfyUI model categories:** Dashboard's `COMFYUI_CATEGORIES` only listed 6 subdirs while ops-controller supports 13. Models downloaded to `clip`, `controlnet`, `embeddings`, `upscale_models`, `diffusion_models`, etc. were invisible in the dashboard and could not be deleted. Now synced with ops-controller's full list.

- **Duplicate webhook deliveries when idempotency_key is NULL:** `mark_outbox_delivered` matched by `idempotency_key`, but `NULL = NULL` is false in SQL, so outbox entries with no key were never marked delivered and re-sent on every cycle. Added `mark_outbox_delivered_by_id` fallback for NULL-key entries.

- **Publish callback accepts invalid status values:** `PublishCallbackBody.status` was an unconstrained `str`; a typo like `"DELIVERED"` would silently mark the job as failed. Now uses `Literal["delivered", "failed"]` for Pydantic validation.

- **Negative tail parameter on service logs:** `tail` query parameter had no lower bound; `tail=-1` could cause undefined Docker SDK behavior. Now clamped to `max(1, min(tail, 500))`.

- **GPU process VRAM always showing 0 on newer pynvml:** `usedGpuMemory` attribute was renamed to `used_gpu_memory` in pynvml >= 12.x. The `getattr` fallback silently returned 0 for all processes. Now checks both attribute names.

- **Dashboard missing comfyui-output volume mount:** The dashboard container set `COMFYUI_OUTPUT_DIR=/comfyui-output` but had no volume mount for it, so `GET /api/orchestration/outputs` always returned empty. Added read-only mount matching the worker's configuration.

- **Misleading readiness and comfyui_delete docstrings:** Readiness endpoint docstring claimed worker health check (never performed); comfyui_delete listed only 5 of 13 valid categories. Both corrected.

- **TOCTOU race in GGUF pull:** Two rapid requests to `/api/models/download` could both pass the `running` guard and spawn concurrent pulls, corrupting status. Now sets `running = True` inside the lock before spawning the thread.

- **Blocking `compute_readiness()` on async event loop:** Readiness and run_workflow endpoints called synchronous HTTP probes (up to 11s) directly, blocking all other requests. Now wrapped in `asyncio.to_thread`.

- **Regression tests for security fixes:** Added tests for token leak prevention in `/api/services`, `PublishCallbackBody` invalid status rejection (422), and `load_template` path traversal prevention.

- **ComfyUI pull output unbounded memory growth:** `_run_comfyui_pull_subprocess` appended every stdout line to a list and re-joined on each line, causing O(n²) memory for long-running pulls (up to 2 hours). Now caps output to the last 50 lines.

- **Param placeholder double-underscore mismatch:** `_normalize_name` replaced each non-alnum char with `_` without collapsing, so `PARAM_INT_my--value` produced `my__value` which never matched user param `my_value`. Now collapses consecutive underscores.

- **SSE multi-line data parsing crash:** `_probe_mcp_tools` only took the first `data:` line from SSE responses; multi-line payloads caused `json.JSONDecodeError`. Now concatenates all `data:` lines before parsing.

- **Non-atomic .env write in ops-controller:** `env_set` endpoint wrote directly to `.env`; a crash mid-write would corrupt the file, breaking all service restarts. Now uses write-to-temp + `os.replace` for crash safety.

- **MCP empty-hint null dereference on poll:** `loadMcpServers()` looked up `mcp-empty-hint` by ID, but the element is a child of `mcp-enabled-chips` and gets destroyed when `innerHTML` is rewritten. On every subsequent 15s poll cycle, `.style.display` threw `TypeError`. Added null guard.

- **Croniter schedules fire at wrong time (timezone bug):** `croniter()` defaulted to naive local time, then `.replace(tzinfo=UTC)` stamped the local value as UTC without converting. On non-UTC machines, schedules fired off by the UTC offset (e.g., 5 hours late in US Eastern). Fixed all 3 call sites to pass `datetime.now(UTC)` as start time.

- **INT param coercion fails on decimal strings:** `int("3.5")` raises `ValueError` in Python, but decimal strings are common from JSON or form fields. Now uses `int(float(value))` to handle inputs like `"20.0"` or `"3.5"`.

- **Docker client leak in ops-controller:** `_docker_client()` created a new Docker SDK client (and HTTP connection pool) on every API call. Now caches a singleton, preventing file descriptor exhaustion under load.

- **Worker cancellation during ComfyUI polling:** `_comfyui_wait_outputs` now checks job state each poll iteration, allowing cancellation to take effect within 3 seconds instead of waiting up to 600 seconds for the full timeout.

- **Model switch partial failure visibility:** `set_active_model` now tracks per-step errors and returns `{"ok": false, "errors": [...]}` when downstream steps (open-webui recreate, agent restart) fail, instead of always returning `{"ok": true}`.

- **Ollama pull loop deadline:** `_run_ollama_pull` had an unbounded `while True` polling loop; now enforces a 2-hour deadline and aborts after 20 consecutive poll errors.

- **ComfyUI pull loop deadline:** `_run_comfyui_pull` had the same unbounded polling pattern; now enforces a 2-hour deadline and aborts after 20 consecutive poll failures.

- **Model gateway inference timeout:** LiteLLM proxy had no explicit timeout configuration, defaulting to 600s (10 minutes). With a 31B model processing 42-50K token contexts, inference regularly exceeds this limit, causing the agent to surface empty responses with `reason=timeout`. Added `request_timeout: 1800` and `stream_timeout: 1800` to both global `litellm_settings` and the chat model's `litellm_params`, aligning with the agent's `idleTimeoutSeconds: 1800`.

- **Service recreate timeout:** `subprocess.run` in ops-controller `service_recreate` now has a 120-second timeout, preventing indefinite hangs if `docker-compose up` stalls. Returns HTTP 504 on timeout.

- **TOCTOU race in `update_job` state validation:** State transition guard read the current state in a separate connection from the write, allowing a concurrent thread to change the state between check and update. Now uses a conditional `UPDATE ... WHERE state IN (valid_sources)` in a single atomic query.

- **Outbox stats inconsistent snapshot:** `get_outbox_stats` ran two independent COUNT queries; rows could change between them. Now uses a single query with `SUM(CASE ...)`.

- **Outbox attempt counter race:** `record_outbox_attempt` read the attempt count and incremented in Python, allowing concurrent calls to read the same value. Now performs the read and update within the same connection/transaction.

- **`cancel_job` re-read via separate connection:** After updating the job state, `cancel_job` called `get_job()` which opened a new connection, potentially returning stale data. Now returns the row from the same connection.

- **Ops-controller health endpoint was a no-op:** `/health` returned `{"ok": true}` unconditionally without checking Docker daemon connectivity. Now pings Docker and returns 503 if unreachable.

- **Stale Docker client cached forever:** If the Docker daemon restarted, the cached client would fail on every subsequent call. Now validates the connection with `ping()` and reconnects on failure.

- **Silent GGUF model scan failure:** `_scan_gguf_models` swallowed `OSError` silently, masking disk/mount failures. Now logs a warning.

- **Model deletion missing audit trail:** `ollama_delete` permanently removed model files without any log entry. Now logs `MODEL_DELETED` with model name and path.

- **Worker creates new TCP connection per ComfyUI poll:** `_comfyui_post_prompt` and `_comfyui_wait_outputs` used bare `httpx.post()`/`httpx.get()`, creating a new connection each call. Now reuses a module-level `httpx.Client` with connection pooling.

- **`_fetch_ollama_library` blocks async event loop:** Synchronous `urllib.request.urlopen` with 15s timeout ran directly in an `async def` route, blocking all other requests. Now wrapped with `asyncio.to_thread`.

- **Ollama library cache race condition:** `_ollama_library_cache` and `_ollama_library_ts` were read/written from both background threads and async handlers without synchronization. Now guarded by `_state_lock`.

- **ComfyUI models.json opened without encoding:** `open(config_path)` used platform-default encoding. Now explicit `encoding="utf-8"`.

- **ComfyUI model delete returns 500 for permission errors:** `PermissionError` is now caught separately and returns HTTP 403 instead of 500.

- **Ops-controller `/services` swallows Docker errors as 200:** Docker failures returned `{"services": [], "error": "..."}` with HTTP 200. Now returns HTTP 503 with `detail` field.

- **Unbounded throughput model-key growth:** `_throughput_samples` and `_ttft_samples` capped samples per model but not the number of distinct models, allowing memory exhaustion via unique model names. Now capped at 50 tracked models.

- **Uncapped `limit` on list_jobs endpoint:** `/api/orchestration/jobs?limit=999999999` could force SQLite to materialize an enormous result set. Now clamped to `max(1, min(limit, 1000))`.

- **Throughput record missing field constraints:** `ThroughputRecordRequest` accepted arbitrary-length model names, negative values, `inf`, and `nan`. Now uses Pydantic `Field` with `max_length`, `ge`, and `le` constraints.

- **ComfyUI history response type validation:** Worker accepted any JSON shape from ComfyUI history endpoint; a non-dict entry would silently poll until timeout. Now validates entry is a dict and logs a warning.

- **Unbounded error strings in job database:** Exception messages from ComfyUI could be arbitrarily large, bloating the SQLite database. Error strings now truncated to 4096 characters before storage.

- **Audit log injection via X-Request-ID:** Raw `X-Request-ID` header was written directly into the JSON audit log, allowing injection of fake fields or broken log parsing. Now sanitized to alphanumeric/dashes, capped at 128 chars.

- **Information disclosure in unauthenticated health/services endpoints:** Docker exception strings (containing hostnames, socket paths, version info) were returned to unauthenticated clients. Now returns generic "Docker unavailable" message; details logged server-side only.

- **`cancel_job` ignored running jobs:** Only `queued` and `validated` jobs could be cancelled, even though the state machine and worker already supported `running -> cancelling`. Now includes `running` in the cancellable states.

- **Benchmark throughput inflated by network overhead:** `output_tokens_per_sec` was calculated from wall-clock time including HTTP round-trip. Now prefers server-reported `timings.predicted_per_second` from llama.cpp when available, falling back to wall-clock calculation.

- **ComfyUI pull polling dies silently on connection loss:** The frontend poll loop had no `.catch()`, causing the button to stay disabled forever if the backend restarted during a download. Now retries up to 20 times before showing a connection-lost message.

- **Service stop/restart missing confirmation:** Stop and restart buttons fired immediately without a `confirm()` prompt, risking accidental service disruption. Now matches the existing pattern for model deletion and active model switching.

- **Cron expression validation missing on schedule update:** `update_schedule` accepted arbitrary cron strings without validation, causing `croniter` to raise at evaluation time. Now validated on save with a 400 response for malformed expressions.

- **Path traversal in ComfyUI node requirements install:** `comfyui_install_node_requirements_api` accepted `..` segments and absolute paths in `node_path`, allowing reads/writes outside the custom nodes directory. Now rejects paths containing `..` or starting with `/`.

- **No validation on `state` query parameter in `list_jobs`:** `GET /api/orchestration/jobs?state=bogus` passed raw strings to the query, producing empty results instead of an error. Now validates against `JobState` enum, returning 400 for unknown states.

- **ComfyUI models scan blocks async event loop:** `_scan_comfyui_models()` performs synchronous filesystem I/O directly in an async route handler, blocking all concurrent requests. Now wrapped with `asyncio.to_thread`.

- **Throughput save on every record call:** `_save_throughput_state()` wrote to disk on every `/api/throughput/record` call, causing unnecessary I/O under high request volume. Now debounced to write at most every 5 seconds via `_maybe_save_throughput()`.

- **SSRF check was a no-op:** `publish_enqueue` detected private IPs in webhook URLs but executed `pass` instead of raising an exception, silently allowing all private-IP webhooks through. Now raises HTTP 400.

- **Blocking DNS lookup in async handler:** `socket.getaddrinfo()` in `publish_enqueue` blocked the event loop during DNS resolution. Now wrapped with `asyncio.to_thread`.

- **Popen without timeout in model pull:** `_run_model_pull` and `_run_gguf_pull` used `proc.wait()` with no timeout; a hung child process would block the pull thread forever. Now uses `proc.wait(timeout=7200)` with `proc.kill()` on timeout.

- **Worker outbox creates new connection per delivery:** `process_outbox()` used bare `httpx.post()` (throwaway client) every 0.5s poll cycle, churning TCP connections. Now reuses a module-level `httpx.Client`.

- **Redundant validated state update in worker:** `execute_job` called `update_job(state=validated)` even though `claim_next_job` already set the state to `validated`, wasting a DB write on every job.

- **`list_outputs` calls `stat()` three times per file:** Sorting, size, and mtime each triggered a separate `stat()` syscall. Now caches the stat result once per file.

- **`update_schedule` re-reads via separate connection:** After UPDATE, `update_schedule` called `get_schedule()` which opened a new connection, potentially returning stale data. Now returns the row from the same connection.

- **Missing index on `workflow_versions.promoted_at`:** `get_promoted_workflow` filtered on `promoted_at IS NOT NULL` without a covering index, forcing a scan of all versions per workflow. Added partial index on `(workflow_id, version DESC) WHERE promoted_at IS NOT NULL`.

- **SSRF bypass via operator precedence:** The SSRF allowlist condition had a Python precedence bug: `and` bound tighter than `or`, causing any dotless hostname (e.g. `http://metadata/`) resolving to a private IP to bypass the check. Fixed parenthesization to correctly scope the Docker hostname exception.

- **Double `stat()` in GGUF model scan:** `_scan_gguf_models` called `p.stat()` twice per file (size + mtime). Now caches the result.

- **Outbox processing runs every poll tick:** `process_outbox()` ran every 0.5s unconditionally, opening a DB connection each time even when no entries existed. Now gated by a 5-second interval (`OUTBOX_CHECK_SEC`).

- **Audit log reads entire file into memory:** `GET /audit` loaded the full audit log (up to 10 MB) via `read_text()` on every request. Now uses `deque(f, maxlen=limit)` to read only the last N lines.

- **Vacuum future silently lost:** `pool.submit(vacuum_db)` discarded the returned future; unexpected exceptions were silently swallowed. Now logs errors via `add_done_callback`.

- **ComfyUI model delete missing audit trail:** `DELETE /api/comfyui/models/{category}/{filename}` performed destructive file deletion with no log entry. Now logs `MODEL_DELETED` with category, filename, and path.

- **MCP server add/remove missing audit trail:** `POST /api/mcp/add` and `POST /api/mcp/remove` modified gateway configuration with no log entry. Now logs `MCP_SERVER_ADDED` and `MCP_SERVER_REMOVED` with the server name.

- **TTFT samples bypass `_MAX_TRACKED_MODELS` cap:** `_ttft_samples` dict grew unboundedly with unique model names even though `_throughput_samples` was capped at 50 keys. Now both dicts share the same cap.

- **Benchmark endpoint bypasses model-key cap:** `/api/throughput/benchmark` inserted into `_throughput_samples` without checking `_MAX_TRACKED_MODELS`, allowing unbounded growth via arbitrary model names. Now guarded.

- **Concurrent model switch race condition:** Two simultaneous `POST /api/active-model` calls could interleave `.env` writes and service restarts, leaving services pointing at different models. Now guarded by an `asyncio.Lock`, returning 409 if a switch is already in progress.

- **Throughput state file encoding mismatch:** `_load_throughput_state` used platform-default encoding while model names may contain non-ASCII characters, causing decode errors on Windows. Now explicit `encoding="utf-8"` on both read and write.

- **Cron expression not validated on schedule creation:** `POST /api/orchestration/schedules` accepted arbitrary cron strings without validation (only `PATCH` validated). Invalid expressions were silently persisted with `next_run=None`. Now validated before creation, returning 400.

- **KeyError crash in benchmark when model cap reached:** `/api/throughput/benchmark` hit a `KeyError` when `_throughput_samples` was at the 50-model cap: the trim-samples line ran unconditionally outside the `if model in` guard. Now properly nested.

- **`update_schedule` preserves stale `next_run_at` on cron change:** Updating a schedule's `cron_expr` via PATCH kept the old `next_run_at`, causing the first fire to use the old timing. Now recomputes `next_run_at` from the new expression.

- **`_write_json_async` not crash-safe:** Agent config and other JSON files were written via `path.write_text()` directly; a crash mid-write would leave truncated JSON. Now uses atomic write-then-rename (matching the throughput state pattern).

- **`list_jobs` limit unsanitized in DB layer:** The routes layer clamped limits, but direct callers of `list_jobs` could pass negative or extreme values. Now clamped to `max(1, min(limit, 1000))` in the DB function itself.

- **Popup-blocked crash in logs viewer:** `window.open` returns `null` when popups are blocked; the next line threw `TypeError` on `win.document.write`. Now checks for null and shows a toast message.

- **Worker `_resolve_workflow_path` missing empty-safe guard:** Workflow IDs consisting entirely of filtered characters (e.g. `"..."`) produced `safe=""`, resolving to `root/.json`. Now returns `None` (matching the dashboard version).

- **`.gguf` filename yields empty `bare_name` in model switch:** `set_active_model` accepted `.gguf` as a valid filename, producing an empty model ID passed to downstream services. Now rejected with 400.

- **ComfyUI history poll uses fixed 3s interval:** `_comfyui_wait_outputs` polled every 3 seconds regardless of render duration, generating ~200 requests over 600s. Now uses exponential backoff (3s → 15s cap).

- **Ollama pull poll retries forever on error:** Frontend `pollOllamaPull` had no error counter; a persistent server error caused infinite 2s polling. Now caps at 20 consecutive errors.

- **Hub model download poll aborts on first error:** A single transient failure stopped polling and left the UI in an indeterminate state. Now retries up to 20 times before giving up.

- **ComfyUI resume poll has no error handling:** `resumeActivePulls` poll chain had no `.catch()`, causing the UI to freeze if a network error occurred. Now handles errors with a 20-retry cap.

- **Audit log response order reversed:** The `deque`-based tail read returned entries in oldest-first order instead of the original newest-first. Now iterates with `reversed(tail)`.

- **`update_schedule_endpoint` crashes if croniter not installed:** The update path caught `(ValueError, KeyError)` but not `ImportError`, causing an unhandled exception. Now matches the create path with `except ImportError: pass`.

- **Vacuum callback double-calls `f.exception()`:** The lambda `f.exception() and logger.error(...)` called `f.exception()` twice, risking `CancelledError` propagation. Now uses a named function that calls it once.

### Added

- **Test coverage expansion (session 4):** Added 3 tests: state transition rejection (published cannot transition to running), cancelled-is-terminal invariant, and `_resolve_workflow_under_root` path traversal prevention (6 attack vectors). Total: 223 tests.

- **Global exception handler:** Unhandled exceptions in API endpoints now return `{"detail": "Internal server error"}` instead of raw Python tracebacks with internal paths and variable values. Full traceback is logged server-side.

- **Test coverage expansion (session 2):** Added 10 new tests covering `/api/services`, `/api/ollama/library`, `/api/throughput/record` (3 cases), `/api/throughput/stats`, `/api/throughput/service-usage`, `/api/auth/config`, and global exception handler (total: 220 tests).

- **GPU Compute Pressure dashboard section:** New `#compute-pressure` section shows per-service VRAM allocation (stacked bar), live process rows, and LLM throughput degradation score. Backend: `GET /api/hardware/gpu-processes` (pynvml + psutil with `pid: host`). Frontend: 3-second polling, color-coded service segments, degradation thresholds (≥85% nominal, 60–84% degraded, <60% starved).

- **SSRF protection on model downloads:** `POST /models/download` now validates URLs against a domain allowlist (HuggingFace, Civitai, GitHub) and blocks private/reserved IP ranges. Prevents server-side request forgery via crafted model URLs.

- **Worker graceful shutdown:** Worker process now handles SIGTERM/SIGINT, drains in-flight jobs (120s timeout), and exits cleanly. Prevents job corruption when Docker stops the container.

- **Test coverage expansion:** Added 88 new tests across 7 test files: ops-controller auth enforcement (26 tests), dashboard auth middleware (18 tests), text sanitizers (16 tests), orchestration outbox/callback (10 tests), ComfyUI API client (9 tests), SSRF validation (5 tests), and model download URL blocking.

### Accessibility

- **WCAG AA contrast fix:** `--muted` color bumped from `#6e7694` to `#8a90a8` (~5.1:1 ratio against `--bg`), clearing the 4.5:1 AA minimum for normal text across ~40 dashboard elements.

- **Keyboard focus visibility:** All `input:focus` rules changed to `focus-visible` pattern — keyboard users see a clear outline ring, mouse users get clean styling. Fixes WCAG 2.4.7.

- **Reduced-motion support:** Added `@media (prefers-reduced-motion: reduce)` that disables animations, transitions, and smooth scroll for users with vestibular disorders. Fixes WCAG 2.3.3.

- **Hardware staleness indicator:** Hardware metrics section fades to 50% opacity and shows a tooltip when the last successful poll is older than 15 seconds, making connectivity loss visible instead of silently showing stale data.

### Changed

- **Compute Pressure overhaul:** `COMPUTE PRESSURE` panel now shows CPU%, RAM%, and (where applicable) VRAM% per toolkit service, sorted by current pressure so the hog is always on top. New ops-controller endpoint `/stats/services` merges `docker stats` with NVML per-PID VRAM. Dashboard proxies via `/api/hardware/service-pressure` (no auth, same pattern as `/api/hardware`). On Windows/WSL2 where per-PID VRAM is unavailable, panel falls back to a single aggregate GPU row. Replaces `/api/hardware/gpu-processes` and the PID-labeling heuristic.

- **Parallel service and dependency probes:** `/api/services`, `/api/health`, and `/api/dependencies` now run all HTTP probes concurrently via `asyncio.gather()` instead of sequentially. Dependency probes converted from synchronous httpx to async. All probes reuse the shared connection-pooled HTTP client.

- **Model Gateway health probe:** Changed from `/health` (returns 401, requires auth) to `/health/liveliness` (unauthenticated) in both `dependency_registry.json` and `services_catalog.py`. Removed stale `ready_url` (`/ready` returned 404). Updated description text.

- **comfyui-mcp healthcheck:** Changed from HTTP GET to `/mcp` (returned 406 and terminated the MCP server) to a TCP socket check on port 9000.

- **Pull endpoint race condition fix:** `/api/ollama/pull` and `/api/comfyui/pull` set `running=True` while holding `_state_lock` before spawning the background thread, closing a TOCTOU race where concurrent requests could bypass the "already running" guard.

- **Improved error messages:** Ops-controller confirm messages now explain what the destructive operation does and the expected JSON shape. Orchestration workflow_id errors include format guidance. Auth error tells user to set the token in `.env`.

- **Orchestration endpoint error handling:** `/api/orchestration/workflows` and `/api/orchestration/outputs` wrapped in `try/except OSError` so filesystem failures return empty lists instead of 500 tracebacks.

- **SQLite durability:** Orchestration DB now uses `PRAGMA synchronous=NORMAL` (was implicit default `FULL` on non-WAL, but `NORMAL` is recommended for WAL mode) and increased `busy_timeout` from 10s to 30s for better contention handling.

- **Worker shutdown WAL checkpoint:** Worker now runs a final `checkpoint_wal()` after draining in-flight jobs during shutdown, ensuring all writes are flushed to the main DB file before exit.

- **Dashboard WCAG AA contrast:** `--muted` color bumped from `#4d5468` (2.73:1 contrast ratio) to `#6e7694` (4.60:1) to pass WCAG AA minimum of 4.5:1 for normal text on dark backgrounds.

- **Auth modal accessibility:** Added Escape key to close, focus trap cycling between input and button, and keyboard event handling.

- **Dashboard UI polish:** Dependencies table simplified (removed empty Ready columns, added Latency column). Logs viewer popup themed to match dashboard. Toasts now click-to-dismiss (5s auto). Nav link "Throughput" renamed to "Telemetry" to match section heading. Ops button loading state uses opacity fade instead of spinning.

- **Frontend auth consistency:** `refreshHardware` and compute pressure used raw `fetch()` bypassing auth headers; switched to `api()` wrapper.

- **Async I/O performance:** Moved all synchronous file reads/writes in async dashboard handlers to `asyncio.to_thread()` via `_read_json_async`/`_write_json_async` helpers. Prevents event-loop blocking during agent config operations.

- **HTTP connection pooling:** Replaced 8 per-request `AsyncClient(timeout=...)` context managers with a persistent `httpx.AsyncClient` managed in the app lifespan. Eliminates TCP handshake overhead on every API call to model-gateway, ops-controller, Qdrant, and MCP gateway.

- **Worker poll interval:** Reduced default `WORKER_POLL_INTERVAL_SEC` from 2s to 0.5s, cutting average job pickup latency by 75%.

- **Frontend polling efficiency:** Added `visibilitychange`-aware polling — all `setInterval` timers (3s compute pressure, 5s hardware, 15s refresh) pause when the tab is hidden and resume on focus. Added `debounce(200ms)` to model search input.

- **Exception handling tightened:** Replaced 17 bare `except Exception:` handlers across orchestration_db.py, rag-ingestion/ingest.py, comfyui-mcp, and orchestration-mcp with specific exception types (`json.JSONDecodeError`, `ValueError`, `OSError`, `ImportError`).

- **AGENTS.md compliance:** Added missing `from __future__ import annotations` to 11 Python files (tests, comfyui-mcp).

- **CI path filter:** Added `rag-ingestion/**` to orchestration-stack-e2E path-gated filter.

- **Docker hardening:** Worker, orchestration-mcp, and ops-controller Dockerfiles now run as non-root `appuser`. Worker Dockerfile upgraded from Python 3.11 to 3.12 for consistency. Added root `.dockerignore` to exclude `.git`, `data/`, `models/`, `.env` from build context (worker uses repo root as context).

- **Reproducible builds:** Pinned model-gateway base image from floating `:main-stable` to `:main-v1.65.5`. Pinned comfyui-mcp upstream clone to specific commit SHA.

- **CI pip caching:** All `setup-python` steps now use `cache: pip` with `cache-dependency-path`, saving 30-60s per CI run.

- **Worker healthcheck freshness:** Worker healthcheck now verifies heartbeat file age (<120s) instead of just file existence, so a deadlocked main loop triggers Docker restart.

- **Worker logging config:** Added `json-file` logging driver with 10MB rotation to worker service (was missing, unbounded logs could fill disk).

- **open-webui startup ordering:** `depends_on` now uses `condition: service_healthy` for llamacpp, model-gateway, and qdrant so open-webui waits for backends to be ready.

- **Dashboard connection pool:** Increased httpx `max_connections` from 20 to 100 to prevent request queuing when multiple browser tabs are open during streaming requests.

- **Frontend polling fix:** `stopPolling()` called before `startPolling()` on tab resume to prevent interval accumulation from rapid visibility changes.

- **Frontend refresh correctness:** `loadThroughputStats()`, `loadThroughputServiceUsage()`, and `loadPerfKPIs()` are now awaited in `refresh()` so the loading spinner stays visible until all data is loaded.

- **Exception handling:** Replaced bare `except Exception: pass/continue` patterns in ComfyUI queue polling (dashboard) and history polling (worker) with specific exception types and debug logging.

- **Hygiene:** Added `pytest-cache-files-*` and `tmp*` to `.gitignore`. Configured `tmp_path_retention_policy = "none"` in pyproject.toml to prevent temp directory buildup.

- **Docker health checks:** Added healthcheck directives for worker (heartbeat file) and comfyui-mcp (process liveness) in docker-compose.yml. Worker poll interval now configurable via `WORKER_POLL_INTERVAL_SEC` env var (default 0.5s).

- **Config validation:** Dashboard port settings now validated at startup with warnings for invalid values and browser-blocked IRC port range (6666-6669).

- **Runtime bootstrap guidance:** The runtime `AGENTS.md` contract was shortened and tightened so the critical agent rules remain inside the bootstrap injection cap. It now explicitly covers prose-only `status` replies, safe `continue`/`resume` behavior, and ComfyUI workflow-authoring expectations without tripping the old truncation threshold.

### Fixed

- **Project identity:** Repository and stack renamed from **AI-toolkit** to **Ordo AI Stack** (technical slug **`ordo-ai-stack`**). Docker Compose **`name`**, image tags (**`ordo-ai-stack-*`**), explicit networks (**`ordo-ai-stack-frontend`** / **`ordo-ai-stack-backend`**), CLI entrypoints (**`./ordo-ai-stack`**, **`.\ordo-ai-stack.ps1`**, **`.\ordo-ai-stack.cmd`**), and **`ORDO_AI_STACK_ROOT`** are updated. **Rebuild** images after pull: `docker compose build` or full init (`ordo-ai-stack initialize`). Old **`ai-toolkit*`** images/networks can be removed once containers are recreated.

- **ComfyUI (GPU):** **`COMFYUI_CLI_ARGS`** in **`.env`** drives **`CLI_ARGS`** (defaults: **`--normalvram`** for GPU **`overrides/compute.yml`**, **`--cpu`** for base compose). **`scripts/detect_hardware.py`** appends **`COMFYUI_CLI_ARGS=--disable-xformers --normalvram --enable-manager`** when missing on NVIDIA/AMD/Intel. Juno **`ltx-video`**: **ImageResizeKJv2** **`cpu` → `cuda`**. OOM: set **`--lowvram`** in **`COMFYUI_CLI_ARGS`** and **`docker compose restart comfyui`**.
- **ComfyUI container RAM cap (GPU):** **`comfyui_memory_limit()`** in **`scripts/detect_hardware.py`** now targets **~42%** of host RAM (floor **32G**, cap **96G**) instead of **25%** / **48G** max — avoids Linux **OOM killer** (**`Killed`** in **`docker logs`** after **`Requested to load VideoVAE`**) on LTX workflows. Override with **`COMFYUI_MEMORY_LIMIT`** in **`.env`**.
- **ComfyUI / LTX Gemma `cudaErrorInvalidValue`:** NVIDIA **`overrides/compute.yml`** — **`PYTORCH_CUDA_ALLOC_CONF`** is **`${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True,pinned_use_cuda_host_register:True}`** so **`.env`** can set **`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`** (omit pinned) when **`sd1_clip.py`** / **`lt.py`** fails on **`torch.cat(...).to(intermediate_device())`**. **TROUBLESHOOTING** documents **`--gpu-only`** as an alternative.

- **MCP gateway — ComfyUI missing from `tools/list`:** With **`--servers`** set, the gateway merges **catalog** files for MCP server definitions and does **not** apply **`--additional-registry`** (registry.yaml) for that purpose. **`gateway-wrapper.sh`** now passes **`registry-custom.docker.yaml`** as **`--additional-catalog`**. The fragment uses the catalog top-level key **`registry:`** (not **`servers:`**) and a proper **`comfyui`** entry (**`type`**, **`title`**, **`description`**, **`env`**). Tavily/DuckDuckGo overrides were removed from the custom file (online catalog + compose env).

- **MCP gateway — `MCP_GATEWAY_VERBOSE`:** **`mcp/gateway/gateway-wrapper.sh`** passes **`--verbose`** to **`docker/mcp-gateway`** when **`MCP_GATEWAY_VERBOSE=1`**. **`TROUBLESHOOTING.md`** documents **`mcp-gateway` listing only 30 tools** when ComfyUI MCP never spawns — root cause of **`gateway__comfyui__*` Tool not found** in the agent.

- **ComfyUI-Manager (Docker):** Seed **`config/comfyui-manager-seed.ini`** into **`data/comfyui-storage/ComfyUI/user/__manager/config.ini`** on first **`ensure_dirs`** ( **`security_level = weak`**, **`network_mode = public`** ) so git installs, pip, and downloads work with **`--listen`**. Compose passes **`GITHUB_TOKEN`** from **`GITHUB_PERSONAL_ACCESS_TOKEN`**. **`ops-controller`** / host scripts use **`python3 -m pip`** for custom-node requirements.

- **MCP Tavily (replaces Playwright):** **`registry-custom.yaml`** registers **`mcp/tavily`** with **`TAVILY_API_KEY`** injected from root **`.env`** (see **`gateway-wrapper.sh`**). Default **`servers.txt`** / **`MCP_GATEWAY_SERVERS`**: **`duckduckgo,n8n,tavily,comfyui`**. Removed **`mcp/playwright`** image build and **`playwright-mcp-image`** compose service.

- **Model Gateway:** `GET /v1/models` no longer lists each Ollama model twice (`name` and `ollama/name`). Only the canonical id is returned (same id the gateway forwards to Ollama), so Open WebUI pickers do not show duplicate HF models.

- **Model Gateway:** Stopped appending placeholder `claude-sonnet-*` model ids to `GET /v1/models` whenever `CLAUDE_CODE_LOCAL_MODEL` was set — they polluted Open WebUI “active models.” Remapping in `/v1/messages` is unchanged. Opt back in with **`CLAUDE_CODE_ADVERTISE_ALIASES=1`** in `.env` if a client strictly validates the model list.

- **MCP module layout:** Gateway templates (`gateway-wrapper.sh`, `registry-custom.yaml`) moved under **`mcp/gateway/`**.

- **Docs — automated social/video pipeline:** `docs/architecture/automated-social-content-pipeline.md` (doc since removed) — target end state (generate → normalize → publish → observe) and how MCP, ComfyUI, n8n, and the dashboard fit together.

- **ComfyUI MCP — stack management tools:** **`comfyui-mcp/tools/management.py`** registers **`install_custom_node_requirements`** and **`restart_comfyui`** (HTTP to ops-controller). **`comfyui-mcp/Dockerfile`** patches upstream **`server.py`** to load them. **`docker-compose`** passes **`OPS_CONTROLLER_URL`** / **`OPS_CONTROLLER_TOKEN`** into **`comfyui-mcp`** and **`mcp-gateway`**. **`mcp/registry-custom.yaml`** + **`gateway-wrapper.sh`** substitute **`PLACEHOLDER_OPS_CONTROLLER_TOKEN`** at gateway startup for spawned ComfyUI MCP containers. **TOOLS.md** / **comfyui-assets** / **TROUBLESHOOTING** document **`gateway__call`** + inner tool names (same paradigm as n8n).

- **`ordo-ai-stack initialize`:** Single entry (`./ordo-ai-stack`, `.\ordo-ai-stack.ps1`, or `.\ordo-ai-stack.cmd`) runs `ensure_dirs`, workspace seeding, then `docker compose up -d --build --force-recreate` from the repo root (set `BASE_PATH` or run from the install directory). **`data/qdrant`** is created by `ensure_dirs` for the RAG profile volume.
- **Housekeeping:** This changelog; PRD milestone updates for M6 (partial, non-auth) and resolved open questions where features already exist (CI, audit rotation, M7 spine).

- **Docs — architecture:** Index at `docs/architecture/README.md` (dir since removed). Removed **`mcp-comfyui-reliability.md`** in favor of a merged ComfyUI/MCP architecture doc — why the stack feels brittle, **`gateway__call`** vs flat tools, Dashboard/n8n alternatives, and the parity matrix.

- **MCP — ComfyUI via gateway only:** Dashboard **`MCP_GATEWAY_SERVERS`** default in **`docker-compose.yml`** now includes **`comfyui`** (with duckduckgo, n8n, playwright) so new installs do not seed **`servers.txt`** with DuckDuckGo-only. **`TOOLS.md`** / **`.example`**, **`TROUBLESHOOTING`**, **`mcp/README.md`**, **`docs/docker-runtime.md`**, **`comfyui-assets.md`**: document valid **`gateway__comfyui__*`** tool names; **`gateway__run_workflow`** is invalid.

- **ComfyUI MCP `workflow_manager`:** Skips UI/editor workflow exports and ignores non-dict top-level keys when scanning `*.json`, so stray metadata files (e.g. `id`/`name` stubs) or Juno UI JSON under `data/comfyui-workflows/` no longer crash server startup.

- **ComfyUI MCP:** `workflow_manager` discovers **`*.json`** recursively under `data/comfyui-workflows/`; **`workflow_id`** may be a **nested POSIX path** (no `.json` suffix). **UI-format** workflow exports are rejected with a clear error; **`/prompt`** requires **API-format** JSON. **TROUBLESHOOTING** documents **`gateway__call`** + **`tool: "run_workflow"`** vs wrong **`gateway__comfyui__run_workflow`** flat tool ids, FL2V vs T2V, and API export.
- **Documentation:** `SECURITY_HARDENING.md` §11 and `.env.example` describe channel SecretRef behavior and Telegram env wiring.
