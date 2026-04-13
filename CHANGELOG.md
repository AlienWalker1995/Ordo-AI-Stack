# Changelog

All notable changes to this project are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Security

- **Timing-safe token comparison:** Auth token verification in dashboard and ops-controller now uses `hmac.compare_digest()` instead of `==`, preventing timing side-channel attacks.

- **LLAMACPP_EXTRA_ARGS injection prevention:** `POST /env/set` now validates `LLAMACPP_EXTRA_ARGS` values with a strict character allowlist, preventing shell injection via backtick/subshell syntax when the value is word-split in run scripts.

- **Hardened CSP headers:** Added `connect-src 'self'`, `frame-ancestors 'none'`, `base-uri 'self'`, and `form-action 'self'` directives to Content-Security-Policy.

- **XSS fix in service cards:** Service hint text was injected as raw HTML; now uses `escapeHtml()`.

- **SSRF protection on outbox webhooks:** `publish_enqueue` endpoint now validates webhook URLs against private/reserved IP ranges using `ipaddress` module, preventing SSRF via crafted callback URLs.

- **Non-root ops-controller container:** ops-controller Dockerfile now runs as `appuser` (UID 1000) with docker group access instead of root.

- **Failed-auth logging:** Dashboard and ops-controller now log `AUTH_FAIL` warnings with path, method, and source IP on every rejected authentication attempt, enabling brute-force detection.

- **Audit log value masking:** `env_set` audit entries now log `len=N` instead of the first 80 chars of the value, preventing credential leakage if token-type keys are ever added to the env allowlist.

### Fixed

- **`ttft_ms` always zero in service-usage:** `/api/throughput/service-usage` was building per-service dicts without `ttft_ms`, so the `last_ttft_ms` stat always reported `0.0`. Now correctly propagated from the raw samples.

- **Worker retry race condition:** Retry job creation used `list_jobs(limit=1)` to find the new job by `retried_from` — with concurrency >1, a different job could occupy the slot, leaving `retry_count=0` and enabling unlimited retries. Now uses `create_job()` return value directly.

- **Dead `:path` route modifier:** `DELETE /api/comfyui/models/{category}/{filename:path}` declared `:path` (allows slashes) but immediately rejected any filename containing `/`. Changed to plain `{filename}`.

- **VACUUM blocks readers under load:** `vacuum_db()` ran with 30s busy_timeout, blocking all dashboard reads during the full rewrite. Now uses 5s timeout and logs a debug message on skip. Worker also skips vacuum when jobs are in-flight.

- **Worker retry crash protection:** If retry logic itself fails (e.g. corrupted compiled_workflow), the failure handler now catches the inner exception and marks the original job as failed instead of crashing the worker thread.

- **OPENCLAW_CONTEXT_WINDOW validation:** Invalid values now log a warning instead of silently falling back, making misconfigurations visible in logs.

- **Workflow version collision:** `save_workflow_version` and `rollback_workflow` used non-atomic SELECT MAX + INSERT for version numbers, allowing collisions under concurrent saves. Now uses atomic `INSERT…SELECT` with `COALESCE`.

- **Throughput state corruption on crash:** `_save_throughput_state()` wrote directly to `throughput.json`; a crash mid-write produced truncated JSON, losing all historical data on next load. Now uses atomic write-then-rename.

- **Job state machine enforcement:** `update_job` now validates state transitions against a defined state machine, preventing invalid regressions (e.g. `published -> queued`). Invalid transitions are logged and silently ignored.

- **Docker client leak in ops-controller:** `_docker_client()` created a new Docker SDK client (and HTTP connection pool) on every API call. Now caches a singleton, preventing file descriptor exhaustion under load.

- **Worker cancellation during ComfyUI polling:** `_comfyui_wait_outputs` now checks job state each poll iteration, allowing cancellation to take effect within 3 seconds instead of waiting up to 600 seconds for the full timeout.

- **Model switch partial failure visibility:** `set_active_model` now tracks per-step errors and returns `{"ok": false, "errors": [...]}` when downstream steps (open-webui recreate, openclaw restart) fail, instead of always returning `{"ok": true}`.

### Added

- **Global exception handler:** Unhandled exceptions in API endpoints now return `{"detail": "Internal server error"}` instead of raw Python tracebacks with internal paths and variable values. Full traceback is logged server-side.

- **Test coverage expansion (session 2):** Added 10 new tests covering `/api/services`, `/api/ollama/library`, `/api/throughput/record` (3 cases), `/api/throughput/stats`, `/api/throughput/service-usage`, `/api/auth/config`, and global exception handler (total: 220 tests).

- **GPU Compute Pressure dashboard section:** New `#compute-pressure` section shows per-service VRAM allocation (stacked bar), live process rows, and LLM throughput degradation score. Backend: `GET /api/hardware/gpu-processes` (pynvml + psutil with `pid: host`). Frontend: 3-second polling, color-coded service segments, degradation thresholds (≥85% nominal, 60–84% degraded, <60% starved).

- **SSRF protection on model downloads:** `POST /models/download` now validates URLs against a domain allowlist (HuggingFace, Civitai, GitHub) and blocks private/reserved IP ranges. Prevents server-side request forgery via crafted model URLs.

- **Worker graceful shutdown:** Worker process now handles SIGTERM/SIGINT, drains in-flight jobs (120s timeout), and exits cleanly. Prevents job corruption when Docker stops the container.

- **Test coverage expansion:** Added 88 new tests across 7 test files: ops-controller auth enforcement (26 tests), dashboard auth middleware (18 tests), text sanitizers (16 tests), orchestration outbox/callback (10 tests), ComfyUI API client (9 tests), SSRF validation (5 tests), and model download URL blocking.

### Changed

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

- **Dashboard UI polish:** Dependencies table simplified (removed empty Ready/OpenClaw columns, added Latency column). Logs viewer popup themed to match dashboard. Toasts now click-to-dismiss (5s auto). Nav link "Throughput" renamed to "Telemetry" to match section heading. Ops button loading state uses opacity fade instead of spinning.

- **Frontend auth consistency:** `refreshHardware` and compute pressure used raw `fetch()` bypassing auth headers; switched to `api()` wrapper.

- **Async I/O performance:** Moved all synchronous file reads/writes in async dashboard handlers to `asyncio.to_thread()` via `_read_json_async`/`_write_json_async` helpers. Prevents event-loop blocking during OpenClaw config operations.

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

- **Config validation:** Dashboard port settings (`OPENCLAW_GATEWAY_PORT`, etc.) now validated at startup with warnings for invalid values and browser-blocked IRC port range (6666-6669).

- **Work summary note - OpenClaw / Primus remediation work:** [`docs/openclaw-primus-work-summary-2026-04-05.md`](docs/openclaw-primus-work-summary-2026-04-05.md) summarizes the recent local work across bridge hardening, flat-tool defaults, dynamic ComfyUI workflow guidance, compaction/runtime investigation, transcript-aware recovery, status/continue reply rules, and the remaining unresolved failure classes.

- **Investigation note — Primus compaction/runtime failures:** [`docs/openclaw-primus-compaction-investigation-2026-04-05.md`](docs/openclaw-primus-compaction-investigation-2026-04-05.md) captures the recent Primus failure pattern with local evidence and external references: OpenClaw compaction/context docs, MCP tool-result sequencing requirements, analogous post-tool synthesis failures, contaminated compaction summaries, empty assistant replies, and the absence of new ComfyUI audio outputs.

- **OpenClaw / Primus remediation summary:** Recent local work now spans three coordinated areas: MCP bridge hardening for local models, a shift from built-in media templates toward dynamic ComfyUI workflow authoring, and runtime guidance/recovery changes aimed at post-tool and post-compaction stability. The new summary note documents the implemented scope and distinguishes the improved failure classes from the ones still unresolved.

- **OpenClaw media workflow model:** ComfyUI media tasks no longer assume built-in `generate_*` templates. Workspace guidance now treats ComfyUI as a general image/audio/video engine: reuse a saved workflow when it fits, otherwise inspect nodes, author or adapt a workflow, validate it, save it if useful, run it, await it, and fetch outputs.

- **OpenClaw MCP bridge defaults:** Generated OpenClaw plugin config now prefers direct `gateway__...` flat tools with `flatTools: true` and keeps `injectSchemas: false`. Workspace guidance was updated to steer agents toward direct workflow-authoring tools such as `gateway__list_workflows`, `gateway__search_nodes`, `gateway__validate_workflow`, `gateway__save_workflow`, `gateway__run_workflow`, `gateway__await_run`, and `gateway__list_outputs` instead of relying on proxy-only `gateway__call`.

- **Runtime bootstrap guidance:** The runtime `AGENTS.md` contract was shortened and tightened so the critical OpenClaw rules remain inside the bootstrap injection cap. It now explicitly covers prose-only `status` replies, safe `continue`/`resume` behavior, and ComfyUI workflow-authoring expectations without tripping the old truncation threshold.

### Fixed

- **OpenClaw post-compaction recovery:** The forked `openclaw-mcp-bridge` now derives recovery context directly from recent session JSONL during `before_prompt_build`. It detects contaminated compaction summaries, empty post-tool assistant turns, and recent raw workflow payloads so resumed turns prefer current structured state over polluted compacted prose.

- **OpenClaw `status` / `continue` reply contract:** Status-style prompts (`status`, `progress`, `update`) and continuation prompts (`continue`, `resume`, `go on`) now receive explicit prose-only reply guards in both workspace guidance and the plugin prompt contract. The model is told not to dump raw workflow JSON, not to repeat the last tool payload, not to end with an empty assistant message, and not to call `read` without an absolute `path`.

- **MCP proxy/tool-call hardening:** `openclaw-mcp-bridge` now strips a stray leading quote before `{`/`[` in malformed proxy args, keeps `tool` required in the proxy schema, and auto-discovers available tools when a proxy call omits `tool` instead of returning a dead-end error. This reduces repeated `missing required tool name` and `args JSON parse failed` loops in local model sessions.

- **OpenClaw runtime bootstrap pressure:** Runtime workspace guidance was shortened so the most important OpenClaw rules are more likely to survive bootstrap truncation. This reduces one source of degraded resumed turns in local-model sessions, even though empty assistant completions and task-path drift are still being observed in later transcripts.

- **Project identity:** Repository and stack renamed from **AI-toolkit** to **Ordo AI Stack** (technical slug **`ordo-ai-stack`**). Docker Compose **`name`**, image tags (**`ordo-ai-stack-*`**), explicit networks (**`ordo-ai-stack-frontend`** / **`ordo-ai-stack-backend`**), CLI entrypoints (**`./ordo-ai-stack`**, **`.\ordo-ai-stack.ps1`**, **`.\ordo-ai-stack.cmd`**), and **`ORDO_AI_STACK_ROOT`** ( **`scripts/validate_openclaw_config.py`** still accepts **`AI_TOOLKIT_ROOT`** ) are updated. **Rebuild** images after pull: `docker compose build` or full init (`ordo-ai-stack initialize`). Old **`ai-toolkit*`** images/networks can be removed once containers are recreated.

- **ComfyUI (GPU):** **`COMFYUI_CLI_ARGS`** in **`.env`** drives **`CLI_ARGS`** (defaults: **`--normalvram`** for GPU **`overrides/compute.yml`**, **`--cpu`** for base compose). **`scripts/detect_hardware.py`** appends **`COMFYUI_CLI_ARGS=--disable-xformers --normalvram --enable-manager`** when missing on NVIDIA/AMD/Intel. Juno **`ltx-video`**: **ImageResizeKJv2** **`cpu` → `cuda`**. OOM: set **`--lowvram`** in **`COMFYUI_CLI_ARGS`** and **`docker compose restart comfyui`**.
- **ComfyUI container RAM cap (GPU):** **`comfyui_memory_limit()`** in **`scripts/detect_hardware.py`** now targets **~42%** of host RAM (floor **32G**, cap **96G**) instead of **25%** / **48G** max — avoids Linux **OOM killer** (**`Killed`** in **`docker logs`** after **`Requested to load VideoVAE`**) on LTX workflows. Override with **`COMFYUI_MEMORY_LIMIT`** in **`.env`**.
- **ComfyUI / LTX Gemma `cudaErrorInvalidValue`:** NVIDIA **`overrides/compute.yml`** — **`PYTORCH_CUDA_ALLOC_CONF`** is **`${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True,pinned_use_cuda_host_register:True}`** so **`.env`** can set **`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`** (omit pinned) when **`sd1_clip.py`** / **`lt.py`** fails on **`torch.cat(...).to(intermediate_device())`**. **TROUBLESHOOTING** documents **`--gpu-only`** as an alternative.

- **MCP gateway — ComfyUI missing from `tools/list`:** With **`--servers`** set, the gateway merges **catalog** files for MCP server definitions and does **not** apply **`--additional-registry`** (registry.yaml) for that purpose. **`gateway-wrapper.sh`** now passes **`registry-custom.docker.yaml`** as **`--additional-catalog`**. The fragment uses the catalog top-level key **`registry:`** (not **`servers:`**) and a proper **`comfyui`** entry (**`type`**, **`title`**, **`description`**, **`env`**). Tavily/DuckDuckGo overrides were removed from the custom file (online catalog + compose env).

- **OpenClaw workspace paths:** Runtime workspace root is the mount root — role docs are **`agents/<name>.md`** (e.g. **`agents/docker-ops.md`**), not **`workspace/agents/…`**. **`TOOLS.md`**, **`AGENTS.md`**, **`docker-ops.md`**, **TROUBLESHOOTING**, and **`.example`** templates updated so agents stop **`read`**/`ENOENT` on **`/app/agents/`** — addresses chat where **all** **`gateway__comfyui__*`** tools were missing and **`gateway__call`** JSON for **`comfyui__pull_comfyui_models`** was documented.

- **MCP gateway — `MCP_GATEWAY_VERBOSE`:** **`mcp/gateway/gateway-wrapper.sh`** passes **`--verbose`** to **`docker/mcp-gateway`** when **`MCP_GATEWAY_VERBOSE=1`**. **`TROUBLESHOOTING.md`** documents **`mcp-gateway` listing only 30 tools** when ComfyUI MCP never spawns — root cause of **`gateway__comfyui__*` Tool not found** in OpenClaw.

- **OpenClaw workspace — `docker-ops.md`:** Documents correct **ComfyUI model pull** MCP ids, **`COMFYUI_PACKS`** / **`comfyui-model-puller`**, **`gateway__call`** JSON, infra checklist when flat tools are missing, and that **`openclaw`** has no **`list-model-packs`** CLI. **`TOOLS.md`** and **TROUBLESHOOTING** include wrong/correct tool tables for LTX / ops issues.

- **ComfyUI-Manager (Docker):** Seed **`config/comfyui-manager-seed.ini`** into **`data/comfyui-storage/ComfyUI/user/__manager/config.ini`** on first **`ensure_dirs`** ( **`security_level = weak`**, **`network_mode = public`** ) so git installs, pip, and downloads work with **`--listen`**. Compose passes **`GITHUB_TOKEN`** from **`GITHUB_PERSONAL_ACCESS_TOKEN`**. **`ops-controller`** / host scripts use **`python3 -m pip`** for custom-node requirements.

- **Workspace — agentic patterns:** [`openclaw/workspace/agents/agentic-design-patterns.md`](openclaw/workspace/agents/agentic-design-patterns.md) maps [Mathews-Tom/Agentic-Design-Patterns](https://github.com/Mathews-Tom/Agentic-Design-Patterns) (tool use, MCP, memory, multi-agent) to OpenClaw + Ordo AI Stack; **`AGENTS.md`** links to it. Cron jobs must use a real **`gateway/…`** model in **`payload.model`**, not **`default`**.

- **OpenClaw built-in `browser` denied by default:** **`merge_gateway_config.py`** adds **`tools.deny: ["browser"]`** unless **`OPENCLAW_ALLOW_BUILTIN_BROWSER=1`**. Web research uses **[Tavily](https://app.tavily.com)** MCP (**`gateway__tavily__tavily_*`**) when **`TAVILY_API_KEY`** is set — not Playwright.

- **MCP Tavily (replaces Playwright):** **`registry-custom.yaml`** registers **`mcp/tavily`** with **`TAVILY_API_KEY`** injected from root **`.env`** (see **`gateway-wrapper.sh`**). Default **`servers.txt`** / **`MCP_GATEWAY_SERVERS`**: **`duckduckgo,n8n,tavily,comfyui`**. Removed **`mcp/playwright`** image build and **`playwright-mcp-image`** compose service.

- **Model Gateway:** `GET /v1/models` no longer lists each Ollama model twice (`name` and `ollama/name`). Only the canonical id is returned (same id the gateway forwards to Ollama), so Open WebUI / OpenClaw pickers do not show duplicate HF models.

- **Model Gateway:** Stopped appending placeholder `claude-sonnet-*` model ids to `GET /v1/models` whenever `CLAUDE_CODE_LOCAL_MODEL` was set — they polluted Open WebUI / OpenClaw “active models.” Remapping in `/v1/messages` is unchanged. Opt back in with **`CLAUDE_CODE_ADVERTISE_ALIASES=1`** in `.env` if a client strictly validates the model list.

- **Docs — MCP hardening + OpenClaw operations:** [`mcp/docs/openclaw-hardening-and-operations.md`](mcp/docs/openclaw-hardening-and-operations.md) — defense-in-depth vs forked bridge; two-layer model (MCP gateway vs dashboard/ops-controller); ComfyUI workflows/models/nodes/monitoring; optional future “dashboard MCP adapter.”

- **MCP module layout:** Gateway templates (`gateway-wrapper.sh`, `registry-custom.yaml`) moved under **`mcp/gateway/`**; ComfyUI/OpenClaw architecture doc moved to **`mcp/docs/comfyui-openclaw.md`** (`docs/architecture/comfyui-openclaw-mcp.md` is a redirect). **`openclaw/openclaw.json.example`** documents **`plugins.entries.openclaw-mcp-bridge`** with a single **`servers.gateway`** URL for the Docker MCP Gateway.

- **Docs — automated social/video pipeline:** [`docs/architecture/automated-social-content-pipeline.md`](docs/architecture/automated-social-content-pipeline.md) — target end state (generate → normalize → publish → observe) and how OpenClaw, MCP, ComfyUI, n8n, and the dashboard fit together.

- **Docs — OpenClaw ↔ ComfyUI vs n8n (merged):** [`docs/architecture/comfyui-openclaw-mcp.md`](docs/architecture/comfyui-openclaw-mcp.md) — reliability (`gateway__call`, flat tools), n8n-style parity matrix, optional ComfyUI-OpenClaw note. Supersedes the split **`mcp-comfyui-reliability`** / **`openclaw-comfyui-n8n-parity`** docs. **`TOOLS.md`** / **`comfyui-assets.md`** updated so agents treat ComfyUI like n8n through the **same MCP gateway**.

- **ComfyUI — local Primus workflows:** [`data/comfyui-workflows/local-primus-replacements/`](data/comfyui-workflows/local-primus-replacements/) — checkpoint-only T2I (`primus_ai_image_local_flux.json`, `PARAM_*` for MCP) and LTX notes (`primus_local_video_ltx_notes.txt`). Docs/agent notes emphasize **local checkpoints** first; Juno pack README rewritten **local-first**. Mirror JSON under [`workflow-templates/comfyui-workflows/local-primus-replacements/`](workflow-templates/comfyui-workflows/local-primus-replacements/).

- **ComfyUI MCP — stack management tools:** **`comfyui-mcp/tools/management.py`** registers **`install_custom_node_requirements`** and **`restart_comfyui`** (HTTP to ops-controller). **`comfyui-mcp/Dockerfile`** patches upstream **`server.py`** to load them. **`docker-compose`** passes **`OPS_CONTROLLER_URL`** / **`OPS_CONTROLLER_TOKEN`** into **`comfyui-mcp`** and **`mcp-gateway`**. **`mcp/registry-custom.yaml`** + **`gateway-wrapper.sh`** substitute **`PLACEHOLDER_OPS_CONTROLLER_TOKEN`** at gateway startup for spawned ComfyUI MCP containers. **TOOLS.md** / **comfyui-assets** / **TROUBLESHOOTING** document **`gateway__call`** + inner tool names (same paradigm as n8n).

- **Dashboard + ops-controller — ComfyUI `pip` from OpenClaw:** **`POST /api/comfyui/install-node-requirements`** (JSON **`node_path`**, **`confirm`**) proxies to ops-controller, which runs **`python3 -m pip install -r`** inside the **`comfyui`** container (Docker API). OpenClaw can manage custom-node Python deps using **`DASHBOARD_AUTH_TOKEN`** + **`wget`/`exec`**, no Docker socket on the gateway. Requires **`OPS_CONTROLLER_TOKEN`**. **`docs/audit/SCHEMA.md`** documents audit action **`comfyui_pip_install`**.

- **ComfyUI asset orchestration:** **`openclaw/workspace/agents/comfyui-assets.md`** — paths (shared `custom_nodes`), what the gateway cannot do (Docker, `pip` in the ComfyUI venv), Dashboard restarts, LiteLLM/`localhost` caveats, cron cleanup. Host scripts **`scripts/comfyui/install_node_requirements.sh`** / **`.ps1`** run **`docker compose exec comfyui python3 -m pip install -r ...`** for a node pack. **`TOOLS.md.example`**, **`AGENTS.md.example`**, **`docker-ops.md`**, **`TROUBLESHOOTING`**, and **`openclaw/README.md`** updated to point agents at this flow instead of looping on **`docker`** errors inside the gateway.

- **OpenClaw MCP bridge fork:** [`openclaw/extensions/openclaw-mcp-bridge/`](openclaw/extensions/openclaw-mcp-bridge/README-ORDO-AI-STACK.md) (based on npm `openclaw-mcp-bridge@0.2.0`) registers **each namespaced MCP tool** as a first-class OpenClaw tool (e.g. `gateway__duckduckgo__search`), not only `gateway__call`. `openclaw-plugin-config` installs from the repo fork when mounted at `/fork-openclaw-mcp-bridge`. After pulling, run `docker compose run --rm openclaw-plugin-config` then restart `openclaw-gateway`.
- **`ordo-ai-stack initialize`:** Single entry (`./ordo-ai-stack`, `.\ordo-ai-stack.ps1`, or `.\ordo-ai-stack.cmd`) runs `ensure_dirs`, OpenClaw workspace seeding, then `docker compose up -d --build --force-recreate` from the repo root (set `BASE_PATH` or run from the install directory). **`openclaw/scripts/ensure_openclaw_workspace.sh`** added for Linux/Mac parity with the PowerShell script. **`data/qdrant`** is created by `ensure_dirs` for the RAG profile volume.
- **OpenClaw channel secrets:** `merge_gateway_config.py` rewrites Discord and Telegram bot tokens to OpenClaw SecretRef form when `DISCORD_TOKEN` / `DISCORD_BOT_TOKEN` or `TELEGRAM_BOT_TOKEN` is set in `.env`, so tokens need not live as plaintext in `openclaw.json`. `openclaw-gateway` receives `TELEGRAM_BOT_TOKEN` from the environment.
- **Housekeeping:** This changelog; PRD milestone updates for M6 (partial, non-auth) and resolved open questions where features already exist (CI, audit rotation, M7 spine).

- **OpenClaw gateway (official image):** Compose no longer passes **`gateway`** as the only command (Docker’s **`node`** entrypoint treated it as **`/app/gateway`** and crashed with **`MODULE_NOT_FOUND`**). **`openclaw-gateway`** and **`openclaw-plugin-install`** now run **`node /app/dist/index.js …`** like [upstream `docker-compose.yml`](https://github.com/openclaw/openclaw/blob/main/docker-compose.yml).

- **openclaw-mcp-bridge (fork):** **`registerFlatMcpTools`** no longer marks registration “done” when **zero** MCP tools were discovered (e.g. **mcp-gateway** still starting). Retries on **`session_start`** up to **12** attempts, then logs and stops. Reduces **`Tool not found` for `gateway__comfyui__run_workflow`** when flat tools never registered.

- **OpenClaw Docker image:** Default compose image is now the official **`ghcr.io/openclaw/openclaw:2026.3.23`** ([release](https://github.com/openclaw/openclaw/releases/tag/v2026.3.23), [package](https://github.com/openclaw/openclaw/pkgs/container/openclaw)) instead of **`ghcr.io/phioranex/openclaw-docker:latest`**. Override with **`OPENCLAW_IMAGE`** in `.env`.

- **Docs — architecture:** Index at [`docs/architecture/README.md`](docs/architecture/README.md). Removed **`mcp-comfyui-reliability.md`** and **`openclaw-comfyui-n8n-parity.md`** in favor of **`comfyui-openclaw-mcp.md`** — why the stack feels brittle, **`gateway__call`** vs flat tools, Dashboard/n8n alternatives, and the parity matrix.

- **MCP — ComfyUI via gateway only:** Dashboard **`MCP_GATEWAY_SERVERS`** default in **`docker-compose.yml`** now includes **`comfyui`** (with duckduckgo, n8n, playwright) so new installs do not seed **`servers.txt`** with DuckDuckGo-only. **`openclaw-gateway`** no longer **`depends_on`** **`comfyui-mcp`** — OpenClaw uses **`http://mcp-gateway:8811/mcp`** only. **`TOOLS.md`** / **`.example`**, **`TROUBLESHOOTING`**, **`mcp/README.md`**, **`docs/docker-runtime.md`**, **`comfyui-assets.md`**: document valid **`gateway__comfyui__*`** tool names; **`gateway__run_workflow`** is invalid.

- **Primus local workflows:** [`local-primus-replacements/README.md`](data/comfyui-workflows/local-primus-replacements/README.md) and [`primus_local_video_ltx_notes.txt`](data/comfyui-workflows/local-primus-replacements/primus_local_video_ltx_notes.txt) drop cloud-model framing; removed `primus_veo3_video_local_ltx_placeholder.txt`. Juno pack [`README.md`](data/comfyui-workflows/juno-comfyui-workflows-main/juno-comfyui-workflows-main/README.md) leads with **local-first** paths. **`TOOLS.md`** / **`AGENTS.md`** (and **`.example`**) + **`comfyui-assets.md`** use **checkpoint vs proxy** language without naming non-local products.

- **OpenClaw workspace:** **`AGENTS.md`**, **`TOOLS.md`**, **`workspace/agents/comfyui-assets.md`** (and **`.example`** templates) distinguish **checkpoint pulls** (`models/`) from **proxy-only** Juno graphs — avoids bogus “model download” heartbeats for HTTP-only paths. **`local-primus-replacements/README.md`** is **local-first** (checkpoints only).

- **ComfyUI MCP `workflow_manager`:** Skips UI/editor workflow exports and ignores non-dict top-level keys when scanning `*.json`, so stray metadata files (e.g. `id`/`name` stubs) or Juno UI JSON under `data/comfyui-workflows/` no longer crash server startup.

- **OpenClaw MCP:** `openclaw-mcp-bridge` uses **one** URL — the Docker **MCP gateway** (`http://mcp-gateway:8811/mcp`). ComfyUI tools are aggregated there; do not add a second `comfyui` server URL. `merge_gateway_config.py` / `add_mcp_plugin_config.py` **remove** a legacy `servers.comfyui` entry if present. **TOOLS.md** / **AGENTS** / **TROUBLESHOOTING** document **`gateway__call`** (and flat **`gateway__comfyui__*`** tools) instead of **`comfyui__call`**.
- **ComfyUI MCP:** `workflow_manager` discovers **`*.json`** recursively under `data/comfyui-workflows/`; **`workflow_id`** may be a **nested POSIX path** (no `.json` suffix). **UI-format** workflow exports are rejected with a clear error; **`/prompt`** requires **API-format** JSON. **TROUBLESHOOTING** documents **`gateway__call`** + **`tool: "run_workflow"`** vs wrong **`gateway__comfyui__run_workflow`** flat tool ids, FL2V vs T2V, and API export.
- **OpenClaw ↔ ComfyUI custom nodes:** `openclaw-gateway` bind-mounts **`data/comfyui-storage/ComfyUI/custom_nodes`** to **`workspace/comfyui-custom-nodes/`** so agents install LTX/Juno/etc. in the same tree the **`comfyui`** service uses (not `/app/ComfyUI` in the gateway image). **`ensure_dirs`** creates the host path; **`TOOLS.md`**, **`AGENTS`**, **`docker-ops.md`**, **`TROUBLESHOOTING`**, **`docs/docker-runtime.md`** updated.
- **OpenClaw workspace:** **`TOOLS.md.example`** and `data/openclaw/workspace/TOOLS.md` rewritten as a **short canonical** contract (MCP, cron+Discord, failure table); long ComfyUI/dashboard runbooks deferred to **`workspace/agents/docker-ops.md`** and **TROUBLESHOOTING**. **`AGENTS.md.example`** and `data/openclaw/workspace/AGENTS.md` gain a **Non-negotiables** section at the top (tool names, Discord source-of-truth, 2000-char limit). **`openclaw/README.md`** and **`docs/configuration.md`** workspace tables updated.
- **Docs:** [TROUBLESHOOTING.md](docs/runbooks/TROUBLESHOOTING.md) adds **OpenClaw cron + Discord** (`not-delivered` vs real Discord failures, `Message failed`, `channel:` recipient); [openclaw/README.md](openclaw/README.md) links to it.
- **Docs:** `docs/docker-runtime.md` OpenClaw dependency table corrected (removed non-existent `openclaw-merge-config` / `openclaw-ensure-workspace`); `docs/configuration.md` adds **Re-run OpenClaw sync** with real compose service names + Windows Git Bash `docker exec` note; `TROUBLESHOOTING.md` quick diagnostics adds the same MSYS path pitfall.
- **OpenClaw AGENTS / MCP tool names:** Verified that **`Tool not found`** for `gateway__duckduckgo__search`, `gateway__n8n__workflow_list`, etc. matches **invalid top-level ids** (upstream registers only **`gateway__call`** per MCP server; this fork also registers flat **`gateway__*__*`** tools), not a missing DuckDuckGo server when **`servers.txt`** lists **`duckduckgo`**. Long **`data/openclaw/workspace/AGENTS.md`** had misleading **`gateway__n8n_*`**-style bullets before OpenClaw’s **~20 k** bootstrap truncation and omitted the explicit wrong-name warning; local AGENTS wording updated; **`TROUBLESHOOTING.md`** documents truncation + **`gateway__n8n__workflow_list`**.
- **OpenClaw native web search:** `openclaw.json.example` and the default data config set **`tools.web.search.enabled: false`** so agents use MCP **`gateway__call` + `duckduckgo__search`** only; **AGENTS.md.example**, **TOOLS.md.example**, **TROUBLESHOOTING**, and **`.env.example`** updated accordingly.
- **OpenClaw workspace permissions:** `openclaw-workspace-sync` now runs **`chown -R 1000:1000`** on the workspace bind mount after seeding so **`MEMORY.md`** and other files are writable by **`node`** (fixes `EACCES` when copies were root-owned). Docs: `TROUBLESHOOTING.md`, `TOOLS.md.example` §H, `openclaw/README.md`; helper scripts `scripts/fix_openclaw_workspace_permissions.ps1` and `.sh`.
- **TOOLS.md stub upgrade:** `openclaw-workspace-sync`, `ensure_openclaw_workspace` scripts, and `fix_openclaw_workspace_permissions` replace a stale short **`TOOLS.md`** with **`TOOLS.md.example`** when the contract marker is missing (opt out: **`OPENCLAW_SKIP_TOOLS_MD_UPGRADE=1`**). Scripts: `openclaw/scripts/workspace_sync_upgrade_tools.sh` (container), `upgrade_tools_md_from_example.ps1` / `.sh` (host).
- **OpenClaw / MCP:** `TOOLS.md.example` adds a **CRITICAL** section (invalid `gateway__*` top-level names, Brave vs DuckDuckGo, why bare `curl`/`GET` to `/mcp` fails); §H adds a matching row. `TROUBLESHOOTING.md` documents these under OpenClaw and avoids implying a plain `curl` to `:8811/mcp` is a valid health probe. Operators with an old short `data/openclaw/workspace/TOOLS.md` should replace it from the template (sync is copy-if-missing).
- **Documentation:** `SECURITY_HARDENING.md` §11 and `.env.example` describe channel SecretRef behavior and Telegram env wiring.
- **OpenClaw docs:** `openclaw/README.md`, `openclaw/OPENCLAW_SECURE.md`, and `openclaw/OPENCLAW_SECURE.md.example` updated for Discord/Telegram `.env` + `merge_gateway_config.py` SecretRef flow.
- **OpenClaw workspace:** Layered `SOUL.md` / `AGENTS.md` / `TOOLS.md` (policy vs environment contract); expanded `TOOLS.md` runbook; optional `USER.md.example`, `IDENTITY.md.example`, `HEARTBEAT.md.example`; `MEMORY.md` guidance; `openclaw-workspace-sync` now copies workspace `*.md` **only when missing** in `data/` (still refreshes `health_check.sh` and `agents/`); `ensure_openclaw_workspace.ps1` seeds the additional files.
- **Git:** Top-level `openclaw/workspace/*.md` (non-example) are gitignored; templates remain as `*.md.example`. Tracked copies of `SOUL.md` / `AGENTS.md` / `TOOLS.md` / `MEMORY.md` were removed from the index (files stay on disk locally).
- **OpenClaw Discord SecretRef:** `merge_gateway_config.py` now emits `provider: "default"` in env SecretRefs (required by OpenClaw 2026.3.x); omitting it caused `channels.discord.token: Invalid input` and prevented the gateway (and Discord) from starting.
- **Docs:** `SECURITY_HARDENING.md` §11, `TROUBLESHOOTING.md` (OpenClaw → Discord/SecretRef), `.env.example`, `SECURITY.md`, and `openclaw/README.md` document the full SecretRef shape and recovery steps.
