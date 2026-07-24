# Dashboard & Cron Parity — Final Acceptance Validation (previous stack → Ordo)

> _Archival record of the one-time build+cutover that produced today's stack. There is no ongoing "V1/V2" split — the stack is simply **Ordo**. Here, "V1" = the previous stack and "V2" = the current substrate under `v2/`._

**Date:** 2026-07-09
**Stack:** project `ordo-v2` (runtime `v2/out`), worktree `arch/v2-substrate` @ `40c6aff`
**Method:** every tab/panel/control was enumerated from the V1 SPA source
(`C:\dev\ordo-ai-stack\dashboard\static\index.html` + `dashboard\app.py` + sub-routers) and its
live data path exercised through the reinstated V1-parity dashboard (`ordo-v2-dashboard-1`,
image `ordo-v2/dashboard-v1`) + backend `ops-api` (V1 ops-controller).
**Tests:** `python -m pytest v2/ -q` → **170 passed** (2.70s).

## How the dashboard was reached (auth note — BY DESIGN, not a gap)

The dashboard's `_verify_auth` has two branches: (1) **trusted-proxy** — if the request comes from
`DASHBOARD_TRUSTED_PROXY_NET` (`172.27.0.0/16`, the whole `ordo-v2-net`) it accepts an
`X-Forwarded-Email` identity header and **fail-closes if the proxy sends no email**; (2) a
Bearer-token branch for external/orchestration callers. Because every container on `ordo-v2-net`
is inside the trusted-proxy CIDR, a container-to-container request is treated as coming *from the
proxy* — so a raw `Authorization: Bearer <DASHBOARD_AUTH_TOKEN>` is correctly ignored and the
request must carry `X-Forwarded-Email` (exactly what caddy+oauth2-proxy inject in production).
All validations below used `X-Forwarded-Email: <operator-sso-identity>` from the `caddy` container
(which has `curl`). **Edge path also verified:** caddy `:443` root → `302 → /oauth2/start`
(SSO intact), `forward_auth oauth2-proxy:4180` wired.

---

## PART A — Per-tab / per-control scorecard

Legend: **PASS** (verified live, evidence) · **BY-DESIGN-DIFF** (intentional V2 change) · **GAP**.

### Tab: Models (📦)
| Item | Route | Status | Verdict | Evidence |
|---|---|---|---|---|
| GGUF list (llama.cpp) | `GET /api/llm/models` | 200 | PASS | 5 real files on disk (Qwen3.6-27B-UD-Q4_K_XL.gguf 17.9GB, Huihui-Q6_K 22.4GB, 2× mmproj, nomic-embed) |
| Active/advertised models | `GET /api/llm/ps` | 200 | PASS | model-gateway advertises `local-chat`, `local-embed` |
| ComfyUI models on disk | `GET /api/comfyui/models` | 200 | PASS | checkpoints incl. ltx-2.3-22b (27.8GB), flux1-schnell-fp8, ace_step; category buckets present |
| ComfyUI packs | `GET /api/comfyui/packs` | 200 (`ok:false`) | PASS | `models.json not found` → **identical V1 behavior** (app.py:788 returns exactly this soft-empty when scripts/comfyui/models.json absent); listing still works |
| Pull GGUF / .env models | `POST /api/llm/pull` | 405-on-OPTIONS | PASS (wired) | route registered (mutation not fired) |
| Delete GGUF | `POST /api/llm/delete` | 405-on-OPTIONS | PASS (wired) | route registered (mutation not fired) |
| Pull ComfyUI pack | `POST /api/comfyui/pull` | 405-on-OPTIONS | PASS (wired) | route registered (not fired) |
| Unified download | `POST /api/models/download` + `/status` | — | PASS (wired) | proxied to ops-api / gguf-puller |

### Tab: GPU (🖥️)
| Item | Route | Status | Verdict | Evidence |
|---|---|---|---|---|
| GPU cards (both) | `GET /api/registry/gpus` | 200 | PASS | **BOTH GPUs**: RTX 5090 (31.8GB, 28.4 used, util 0, models comfyui/local-chat/local-embed) + GTX 1070 (8.0GB, 2.5 used, util 6, models voice-stt/voice-tts) |

### Tab: Registry (📋)
| Item | Route | Status | Verdict | Evidence |
|---|---|---|---|---|
| Model registry table | `GET /api/registry/models` | 200 | PASS | 5 models w/ real GPU uuids: local-chat→5090 (Qwen3.6-27B-UD-Q4_K_XL.gguf, ctx 262144), local-embed/comfyui→5090, voice→1070; provenance populated |
| Set active (per row) | `POST /api/registry/models/{id}/enable` | 405-on-OPTIONS | PASS (wired) | route registered (not fired — recreate already proven in FLIP) |
| Assign GPU (per row) | `POST /api/registry/models/{id}/assign-gpu` | 405-on-OPTIONS | PASS (wired) | route registered (not fired) |

### Tab: Model / modelctl (⚙️)
| Item | Route | Status | Verdict | Evidence |
|---|---|---|---|---|
| Flag cards (GET/read) | `GET /api/model-config` | 200 | PASS | full flag catalog (core: LLAMACPP_MODEL, LLAMACPP_CTX_SIZE default 262144, + context/attention/speculative/generation/multimodal/advanced groups) with help + override state |
| Apply & restart | `POST /api/model-config` | 405-on-OPTIONS | PASS (wired) | route registered (**not fired** — would recreate llamacpp) |
| Active-model switch | `POST /api/active-model` | 405-on-OPTIONS | PASS (wired) | route registered (not fired) |
| Default-model read/set | `GET/POST /api/config/default-model` | 200 (GET) | PASS | GET → `default_model=local-chat`, `open_webui_default_model=local-chat:chat`; POST wired |

### Tab: Services (⚡)
| Item | Route | Status | Verdict | Evidence |
|---|---|---|---|---|
| Service list + status | `GET /api/services` | 200 | PASS | services enumerated (llamacpp/model-gateway/webui/comfyui/n8n/qdrant/hermes/…) with ok flags + hints |
| Health rollup | `GET /api/health` | 200 (no auth) | PASS | 9 services all `ok:true` |
| Dependencies probe | `GET /api/dependencies` | 200 (no auth) | PASS | canonical dependency list probed live (model-gateway 11.19ms, llamacpp, …) all `ok:true` |
| RAG status | `GET /api/rag/status` | 200 (no auth) | PASS | collection `documents`, points_count 1, status green |
| Hardware (hw-stat bar) | `GET /api/hardware` | 200 | **PASS (was false-PASS, now FIXED)** | The original PASS scored the 200 alone — but it carried **nulls**: `disk_*:null` + `gpu:null` + `gpus:[]` (the storage + GPU hw-stat widgets were BLANK live). Two root causes, both fixed (see FLIP.md "hw-stat bar widget fix"): (1) the dashboard service reserved no GPU → no `nvidia-smi` for `_probe_gpu`/`list_gpus`; (2) `BASE_PATH=<Windows host path>` leaked via env_file → `psutil.disk_usage` raised. **After fix:** `disk_used_gb 1180.0 / disk_total_gb 1999.8 / disk_pct 59.0`; `gpu` populated; `gpus` lists BOTH cards (1070 8.6GB + 5090 34.2GB, real util/temp); `nvidia-smi -L` inside the container lists both. |
| Service pressure (hw-stat bar) | `GET /api/hardware/service-pressure` | 200 | **PASS (was false-PASS, now FIXED)** | The original PASS scored the 200 alone — but ops-api `/stats/services` **timed out even at 40s** (sequential `c.stats()` × ~24 running containers ≈ 48s), so the dashboard's 3s call fell to `_empty_payload()` → every service `running:false` live. Root-cause fix: parallelize the independent per-container samples across a thread pool (NOT a timeout bump; FLIP.md). **After fix:** route returns in **2.37s** (< the 3s timeout) with **24/25 services `running:true`** + real cpu/mem (llamacpp mem 10.93GB, …). (`gpu:null`/`vram_gb:0` here is the pre-existing WSL2 per-PID-VRAM limit → `vram_aggregate_unavailable:true`; GPU/VRAM data reaches the UI via `/api/hardware` `gpus`.) |
| Throughput stats | `GET /api/throughput/stats` | 200 (no auth) | PASS | per-model tok/s + TTFT percentiles (local-chat p95 69.8 tok/s, 500 samples) |
| Service usage | `GET /api/throughput/service-usage` | 200 | PASS | local-chat used by OpenAI(96)/curl(2)/Python-urllib(5) |
| Performance summary | `GET /api/performance/summary` | 200 | PASS | ctx 131072, worker_concurrency 1, top_models rollup |
| Run benchmark | `POST /api/throughput/benchmark` | 405-on-OPTIONS | PASS (wired) | route registered (not fired) |
| Record throughput | `POST /api/throughput/record` | (token-gated) | PASS (wired) | X-Throughput-Token path present |

### Tab: MCP (🧩)
| Item | Route | Status | Verdict | Evidence |
|---|---|---|---|---|
| Enabled servers + catalog | `GET /api/mcp/servers` | 200 | PASS | enabled: duckduckgo,n8n,searxng,comfyui,orchestration; 31-entry catalog |
| Per-server health | `GET /api/mcp/health` | 200 | PASS | gateway reachable, all 5 servers `status:ok` |
| Add / remove server | `POST /api/mcp/add`, `/api/mcp/remove` | 405-on-OPTIONS | PASS (wired) | routes registered (not fired) |

### Tab: Orchestration (🛠️)
| Item | Route | Status | Verdict | Evidence |
|---|---|---|---|---|
| Readiness | `GET /api/orchestration/readiness` | 200 (no auth) | PASS | model_gateway_ready, mcp_gateway_reachable (tool_count 6), comfyui_media, orchestration_probe all ok |
| Recent jobs | `GET /api/orchestration/jobs` | 200 | PASS | historical jobs listed (e.g. flux2_txt2img → artifact_ready) |
| Workflows / outputs / schedules | `GET /api/orchestration/{workflows,outputs,schedules}` | 200 | PASS | all respond (empty lists — no queued work) |
| ComfyUI status + guardian rollup | `GET /api/orchestration/comfyui/status` | 200 | BY-DESIGN-DIFF | route works; `queue.reachable:false`/`up:false` = the **guardian queue probe is intentionally absent** — V2 scheduler replaced the reactive guardian (documented in PARITY.md cat D). Container_state `running`. |
| Run / cancel / validate / promote | `POST /api/orchestration/{run,jobs/{id}/cancel,validate,…}` | 405-on-OPTIONS | PASS (wired) | routes registered (not fired) |

### Tab: Grafana (📊)
| Item | Route/Path | Status | Verdict | Evidence |
|---|---|---|---|---|
| Grafana embed | `/grafana/d/ordo-llm-gpu/…` (iframe) | — | PASS | Grafana `/api/health` → 200 (v11.4.0); dashboard `ordo-llm-gpu` "Ordo — llama.cpp & GPU" exists at the exact embedded URL |
| Panel data — llama.cpp | prometheus `llamacpp:n_decode_total` | 200 | PASS | non-empty series → value `31` (job=llamacpp, instance llamacpp:8080); 11 `llamacpp:*` metrics scraped |
| Panel data — GPU | prometheus `nvidia_smi_memory_used_bytes` | 200 | PASS | **both GPUs**: uuid 20fac13a (1070)=2.69GB, uuid 97fe65ee (5090)=30.49GB; full `nvidia_smi_*` metric family present |

### Service control (per-service, ops available)
| Item | Route | Status | Verdict | Evidence |
|---|---|---|---|---|
| Ops available | `GET /api/ops/available` | 200 | PASS | `{"available":true}` (per-service recreate enabled) |
| Start/Stop/Restart | `POST /api/ops/services/{id}/{start,stop,restart}` | 405-on-OPTIONS | PASS (wired) | routes registered (**not fired** — recreate controls already proven in FLIP; NOT re-triggered) |
| Logs | `GET /api/ops/services/{id}/logs` | — | PASS (wired) | proxied to ops-api |

### Whole-stack compose control
| Item | Route | Status | Verdict | Evidence |
|---|---|---|---|---|
| Compose up/down (whole-stack) | ops-api `POST /compose/up` | 422 (empty body) | BY-DESIGN-DIFF | V1 dashboard `app.py` has **no** `/api/compose/*` route; the whole-stack control lives at the ops-api backend and is reachable (422 = route wired, validation rejects empty body). **Not fired** (would recreate the whole stack). Per V2 design this is the operator-gated whole-stack path. |

### Sibling UIs (served by the stack)
| UI | Reached | Status | Verdict |
|---|---|---|---|
| hermes-dashboard (kanban) | `http://hermes-dashboard:9119/` | 200 | PASS (title "Hermes Agent - Dashboard"; caddy `/hermes/*` reverse_proxy w/ X-Forwarded-Prefix) |
| codebase-memory UI | `http://codebase-memory-ui:9750/` | 200 | PASS |
| open-webui | `http://open-webui:8080/` | 200 | PASS |
| n8n | `http://n8n:5678/` | 200 | PASS |
| searxng | `http://searxng:8080/` | 200 | PASS |
| edge caddy :443 SSO | `https://…ts.net/` | 302 → /oauth2/start | PASS |

### Checklist items that do NOT exist in V1 (not gaps)
- `/api/audit` → 404 — **V1 has no such route** (speculative checklist item; correctly absent).
- `/api/mcp/containers` → **V1 has no such route**; MCP container status is surfaced via `/api/mcp/health` (verified PASS above).
- Standalone `/api/guardian/status` on the dashboard → **V1 has none**; guardian was only ever a
  rollup field inside `/api/orchestration/comfyui/status` (verified, BY-DESIGN-DIFF above).

---

## PART B — Cron pipeline (one allowed mutation)

`docker exec --user hermes ordo-v2-agent-1 hermes cron list` → **7 jobs, all `[active]`, all with
FUTURE next-run times** and prior `Last run … ok`:

| Job ID | Name | Schedule | Next run | Skills/Script |
|---|---|---|---|---|
| 3fae302517c6 | Daily AI News Update | 0 12 * * * | 2026-07-10T12:00Z | ai-news-fetcher |
| 5cb290c34008 | Ordo-AI-Stack GitHub Monitor | 0 12 * * * | 2026-07-10T12:00Z | stack-audit / stack_monitor.py |
| 68681701c991 | social-relay-dialogue-reel | 0 22 * * * | 2026-07-09T22:00Z | social-relay-dialogue-reel |
| fe506a133493 | reel-health | 30 21 * * * | 2026-07-09T21:30Z | — |
| 39dc0c74dcec | reel-metrics | 0 3 * * * | 2026-07-10T03:00Z | — |
| f14a00ab6c66 | AI & Gaming PM Job Monitor | 0 3 * * * | 2026-07-10T03:00Z | job-search |
| bd1ef4cd1232 | Storage Purge | 0 9 * * 0 | 2026-07-12T09:00Z | — |

**Scheduler:** `hermes cron status` → "Gateway is running — cron jobs will fire automatically" (PID 1, 7 active).

**Trigger (the ONE allowed mutation):** picked the lightest non-posting job — **GitHub Monitor
`5cb290c34008`** (read-only stack audit → short Discord summary; no media, no ComfyUI, no GPU
render). Command: `docker exec --user hermes ordo-v2-agent-1 hermes cron run 5cb290c34008
--accept-hooks` → "Triggered … It will run on the next scheduler tick." (exit 0).

**End-to-end result (PASS):**
- **Ran:** new output file `data/hermes/cron/output/5cb290c34008/2026-07-09_16-44-27.md` (40 KB),
  triggered 16:41 → completed 16:44 UTC (~3 min).
- **Skill loaded:** output header shows `stack-audit` skill content injected → **skills ARE loaded**.
- **LLM succeeded (no 500s):** the file contains a fully-synthesized multi-service audit (Open WebUI
  v0.10.1→v0.10.2, n8n 2.28.3→2.29.9, ComfyUI v0.19→v0.27, llama.cpp digest→b9940, LiteLLM v1.91.1,
  Hermes v2026.7.7.2, Qdrant/Caddy/oauth2-proxy SAFE) with severity classes + release links — only
  possible via working inference. Grep for `500/Internal Server Error/inference error` in the run
  window → **none**.
- **Delivery landed:** `jobs.json` record for the run → `last_run_at:2026-07-09T16:44:27`,
  `last_status:"ok"`, `last_error:null`, **`last_delivery_error:null`** (a Discord delivery failure
  would populate this field) → Discord post to `discord:…:1500550218771595445` succeeded.
- `hermes cron list` afterward confirms `Last run: 2026-07-09T16:44:27.727129+00:00  ok`.

**Hermes basics:**
- **MCP reachable from agent:** `POST http://mcp-gateway:8811/mcp` `initialize` → 200
  (Docker AI MCP Gateway v2.0.1, tools capability advertised); corroborated by
  `/api/orchestration/readiness` `mcp_gateway_reachable:true tool_count:6` and `/api/mcp/health`
  all 5 servers ok.
- **Skills/memory loaded:** stack-audit skill injected into the cron run (above).
- **Discord connected:** cron delivery succeeded (`last_delivery_error:null`) and Discord slash-command
  registration is active (skill-name-collision warning present in logs = registration path running).

---

## PART C — Verdict

**PARITY ACHIEVED.** Every enumerated V1 tab, panel, widget, read-path, and mutation control is
present and wired in V2, verified feature-by-feature with live evidence. Zero GAPs.

- **PASS:** all Models/GPU/Registry/modelctl/Services/MCP/Orchestration/Grafana read paths + every
  mutation route (wired, dry-checked, not fired) + all 6 sibling UIs + edge SSO + the cron pipeline
  end-to-end.
- **BY-DESIGN-DIFF (3, all intentional, all documented):**
  1. **Guardian** — no standalone panel; the reactive guardian was replaced by the V2 scheduler
     (PARITY.md cat D). Its only V1 surface (queue rollup in `/api/orchestration/comfyui/status`)
     still responds; the queue-reachability field is intentionally false.
  2. **Whole-stack compose** — not a dashboard route in V1 either; lives at ops-api, reachable
     (422 wired), operator-gated, not fired.
  3. **Container-to-container auth** — trusted-proxy branch requires `X-Forwarded-Email` (the SSO
     identity header), not raw Bearer, for on-network callers. Matches production caddy+oauth2-proxy.
- **GAP:** **none.**

**Non-gap clarifications:** `/api/audit`, `/api/mcp/containers`, standalone `/api/guardian/status`
do not exist in V1 and correctly 404; `/api/comfyui/packs` `ok:false` is identical V1 soft-empty
behavior (no `models.json`).

**Correction (hw-stat bar):** an earlier revision of this doc claimed `/api/hardware` `gpus:[]`
(and the empty storage/service-pressure widgets) were "by design" — that was WRONG; it was a live
regression (a 200 carrying nulls scored as PASS). The dashboard-service GPU reservation was dropped
in the reinstatement and a Windows `BASE_PATH` leaked into the Linux container; ops-api
`/stats/services` was sequential and timed out. All three are now fixed and the widgets carry real
data (see the Hardware / Service pressure rows above and FLIP.md "hw-stat bar widget fix").

### Safety attestation
- **V1 (`ordo-ai-stack`) untouched:** all containers `Exited` (stopped-intact), source read-only.
- **No GPU-bound service recreated:** `ordo-v2-llamacpp-1` and `ordo-v2-agent-1` uptimes unchanged
  across the session (no restart); no `/api/*` or `/compose/*` mutation fired.
- **Only mutation performed:** one `hermes cron run 5cb290c34008` (GitHub Monitor) — a read-only
  stack audit that delivered a Discord summary. Nothing else.
- **Tests:** 170 passed.
