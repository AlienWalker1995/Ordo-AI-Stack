# FLIP — the exact next-phase cutover for this box (RTX 5090 + GTX 1070)

Phase 4 (build images, stage data/config, preflight GO) is **done** — see the bottom of this file
for what was staged and when. This file is the **operator's** Phase 5 runbook: the atomic flip.
Every command below was rendered/validated against the real box; run them **in order**.

Preconditions already satisfied by Phase 4 (verified):
- All 9 `ordo-v2/*` project images built; the patched llama.cpp image present; every upstream image
  pre-pulled (flip needs **no network**). `ordo preflight … → GO`.
- Mutable data staged to the V2 data root `C:\dev\ordo-v2\data`; immutable big content (GGUF models,
  ComfyUI model caches) **shared by path** read-only from `C:\dev\ordo-ai-stack` (never copied).
- `C:\dev\ordo-v2\v2\out\` is a self-contained runtime dir: rendered `.env` + `docker-compose.yml`,
  `ordo.yaml` (for in-place re-render), `secrets.env` (gitignored, real values), `auth/`,
  `monitoring/`, and `mcp/` (the mcp-gateway's wrapper-native `servers.txt` + `registry-custom.yaml`).
- V2 named volumes `ordo-v2_grafana-data` / `ordo-v2_prometheus-data` pre-populated from V1 history.

Precondition added by **Phase 5.5** (the offline parity audit, after the #1/#2 rollbacks):
- **`v2/AUDIT.md` has ZERO unresolved GAPs.** Every V2 service was diffed against its live V1
  container across every runtime dimension and all 7 gaps (4 classes) were fixed in the render
  engine/manifests with a regression test per class. `docker compose config` (all 10 profiles) →
  exit 0; `ordo preflight` → GO (parity 24/24, 0 mismatch); 134 tests + ruff green. The two
  render-defects that rolled back #1 (llamacpp launch) and #2 (agent launch) are joined by the
  **primary-GPU-uuid pin** (llamacpp/comfyui/llamacpp-embed had `count: all` — a WSL2 no-op that
  would have leaked compute onto the 1070) and the **mcp-gateway wiring** (docker.sock + rendered
  catalog) as newly-closed pre-flight gaps. Re-render `out/` before the flip if `ordo.yaml` changed.

All commands run from Git Bash with `export MSYS_NO_PATHCONV=1`, or from PowerShell (paths work as-is).

---

## 1. Stop V1 Hermes ONLY, then final delta-sync the brain

Hermes writes to `data\hermes` continuously; the Phase-4 copy is a staging snapshot. Freeze it, then
sync the delta. Stopping just the two Hermes containers keeps the rest of V1 up during the sync.

```bash
# freeze the agent so the brain stops changing mid-copy
docker compose -p ordo-ai-stack stop hermes-gateway hermes-dashboard

# final delta-sync: /MIR mirrors (adds new, updates changed, deletes removed) EXCLUDING transient
# caches. Robocopy exit codes 0-7 are success.
robocopy "C:\dev\ordo-ai-stack\data\hermes" "C:\dev\ordo-v2\data\hermes" /MIR /R:1 /W:1 \
  /XD audio_cache image_cache
```

> Using `/MIR` (not `/E`) for the delta so a file the agent DELETED since staging is also removed
> from the V2 copy — a true mirror. Keep the `/XD` excludes identical to the Phase-4 copy.

## 2. Stop the rest of V1 — `stop`, NOT `down` (most conservative rollback)

```bash
docker compose -p ordo-ai-stack stop
```

`stop` (not `down`) leaves V1's containers, networks, and volumes intact — rollback is a single
`start`. **Do NOT `down`, prune, or delete any `ordo-ai-stack` volume/image.** That is the rollback
asset. (V1's `make up` decrypts SOPS secrets; you are only *stopping*, so no secret handling here.)

## 3. Bring V2 up — core first, then profiles

```bash
cd C:\dev\ordo-v2\v2\out

# 3a. core control plane (no host ports; can't clash with a still-resident V1)
docker compose -p ordo-v2 up -d

# 3b. add the operator's profiles once core is healthy. The edge (Caddy :443) is the ONE host-port
# publish — bring it up LAST, only after V1 is stopped, so :443 is free.
docker compose -p ordo-v2 \
  --profile rag --profile webui --profile automation --profile search \
  --profile codebase-memory --profile hermes-ui --profile monitoring \
  --profile media --profile voice \
  up -d

docker compose -p ordo-v2 --profile edge up -d      # front door LAST (binds host tailnet :443)
```

> All profiles at once is fine too (`--profile media --profile voice … --profile edge up -d`);
> the staged order above just lets you confirm core health before the GPU-heavy media/voice come up.
> The GPU is shared — the V2 **scheduler** (not the old reactive guardian) admits media co-run when
> it fits, so you don't need to hold media off manually.

## 4. Validation checklist (exercise the REAL paths before trusting the flip)

```bash
# chat completion via the model-gateway (the local-chat alias, ctx=131072)
docker compose -p ordo-v2 exec model-gateway \
  curl -sf http://localhost:11435/v1/chat/completions \
  -H "Authorization: Bearer local" -H 'Content-Type: application/json' \
  -d '{"model":"local-chat","messages":[{"role":"user","content":"ping"}],"max_tokens":8}'

# MCP tool call — gateway loaded a non-empty catalog (qdrant_search, searxng_web_search, …)
docker compose -p ordo-v2 exec mcp-gateway /healthcheck.sh    # or list tools via the gateway API

# cron fires — Hermes scheduler loaded the staged jobs.json (daemon owns it; check it's ticking)
docker compose -p ordo-v2 logs --tail=50 agent | grep -i cron

# dashboards reachable behind the edge (SSO): control-plane, /hermes/, /grafana/, /n8n/, /codebase-memory/
#   https://${CADDY_TAILNET_HOSTNAME}/         (control-plane dashboard)
#   https://${CADDY_TAILNET_HOSTNAME}/grafana/ (metrics — V1 history present)

# GPU scheduler status (co-run/admission state)
docker compose -p ordo-v2 exec ops-controller curl -sf http://localhost:9000/status

# voice on the 1070 (STT + TTS must land on the Pascal card — 5090 has no kernels for them)
docker compose -p ordo-v2 exec stt curl -sf http://localhost:8000/v1/models
docker compose -p ordo-v2 exec tts curl -sf http://localhost:8880/v1/audio/voices
docker inspect ordo-v2-stt-1  --format '{{range .HostConfig.DeviceRequests}}{{.DeviceIDs}}{{end}}'  # -> 1070 uuid
docker inspect ordo-v2-tts-1  --format '{{range .HostConfig.DeviceRequests}}{{.DeviceIDs}}{{end}}'  # -> 1070 uuid

# monitoring scrape — prometheus is scraping llamacpp:8080/metrics + gpu-exporter:9835
docker compose -p ordo-v2 exec prometheus \
  wget -qO- 'http://localhost:9090/api/v1/targets' | grep -o '"health":"[a-z]*"' | sort | uniq -c
```

Confirm: chat returns tokens, MCP catalog non-empty, cron ticking, all dashboards load behind SSO,
`/status` shows the scheduler, STT/TTS answer AND are pinned to the 1070 uuid, prometheus targets `up`.

## 5. Rollback (one command each way) — if ANYTHING is wrong

```bash
docker compose -p ordo-v2 stop            # stop V2 (keeps its volumes/data intact for a retry)
docker compose -p ordo-ai-stack start     # V1 returns, unchanged (rollback asset was never touched)
```

If V2's edge grabbed :443, `stop`ping V2 frees it before V1 restarts. V1 comes back on the exact
state it was `stop`ped in. (If V1 needs its SOPS secrets refreshed after a long downtime, use the
V1 `make up` on the host — but a plain `start` reuses the already-decrypted runtime env.)

## 6. Decommission (days later, only once V2 is trusted)

Only after V2 has earned it end-to-end: `docker compose -p ordo-ai-stack down` then prune V1's
containers/volumes/images. **Not before** — until then V1 is your instant rollback.

---

## Phase-4 staging record (what this branch prepared)

- **V2 data root:** `C:\dev\ordo-v2\data` (set via `site.DATA_PATH` in `v2/ordo.yaml` → rendered `.env`,
  so binds resolve deterministically, not relative to `out/`).
- **Shared by path (read-only, NOT copied):** `C:\dev\ordo-ai-stack\models\gguf` (GGUF weights),
  `C:\dev\ordo-ai-stack\models\comfyui` (~30GB caches), `C:\dev\ordo-ai-stack\data\comfyui-storage`
  (ComfyUI app + custom nodes). Rationale: immutable content; copying 50GB+ is waste.
- **Copied (mutable staging), byte counts at staging time (2026-07-09T14:02Z):** hermes 2.417 GB,
  comfyui-output 4.072 GB, drafts 3.166 GB, n8n-data 1.031 GB, voice 927 MB, open-webui 287 MB,
  qdrant 164 MB, primus_relay 95 MB, plus n8n-files/searxng/drafts-pending/social-relay/rag-input/
  voices/dashboard. Named volumes copied into `ordo-v2_grafana-data` (14 MB) + `ordo-v2_prometheus-data`
  (57 MB). Hermes `audio_cache`/`image_cache` intentionally excluded.
- **Secrets:** `v2/out/secrets.env` (gitignored), 11 keys populated from V1 `runtime/.env` +
  `runtime/secrets/*`. Delta-sync in step 1 does NOT touch secrets.
- **Preflight:** `GO` — parity vs live `.env` (24 keys, 0 mismatch), all project images built,
  all secrets set, ctx consistent (131072), model sha256-pinned, MCP images digest-pinned.

---

## Phase-5 EXECUTED — attempt #1: ROLLED BACK (2026-07-09), then root-cause fixed

**Outcome: clean rollback. V1 fully restored and serving. A render-engine defect that blocked
V2's llamacpp was found live, and FIXED upstream in this same commit — the next attempt should pass.**

### Timeline (UTC)
- `14:14:14` — stopped V1 hermes-gateway + hermes-dashboard; robocopy `/MIR` delta-sync of the brain
  into `C:\dev\ordo-v2\data\hermes` (exit 1 = success; 18 files updated, 0 failed, 0 extras);
  cleared 4 stale locks in the V2 copy only (gateway.lock/pid, auth.lock, shell-hooks-allowlist.json.lock).
- `14:14:33` — **V1 full stop** (`docker compose -p ordo-ai-stack stop`). 0 V1 running; 5090 VRAM → 4 MiB.
- `14:15`    — V2 core up (`up -d`): 6 containers started.
- `14:16`    — **defect detected:** V2 `llamacpp` booted in llama.cpp **router mode** (`Available models (0)`,
  5090 still 4 MiB, no healthcheck). Root cause below.
- `14:18:26` — **ROLLBACK:** `docker compose -p ordo-v2 stop` (edge never came up → :443 never contended).
- `14:18:36` — `docker compose -p ordo-ai-stack start`. (One transient `network … not found` on start —
  a stale ref as `ordo-v2-net` was torn down same session; healed, no container left down.)
- `~14:21`   — V1 llamacpp `model loaded` → `/health 200`, 5090 back to **29711 MiB** (normal resident).
- V1 chat proof: `POST local-chat` → **HTTP 200**, `model:"local-chat"`, `system_fingerprint:"b1-86b9470"`
  (patched build), spec-decode live (draft_n=21, accepted=12). All **24** V1 containers healthy again
  (gpu-exporter `unhealthy` is pre-existing on driver 581.80, not a regression).

**Total production downtime ≈ 6.5 min** (14:14:33 stop → ~14:21 V1 serving again).

### Root cause (the real fix, not a bandaid)
`ordo/compose.py` built the `llamacpp` service with **only** `command: [--metrics]` and **no** entrypoint
override and **no** volumes. The patched image `…llamacpp-patched:qwen36-swa-86b9470` is a drop-in *binary*
(`ENTRYPOINT ["llama-server"]`); the launch LOGIC lives in the host wrapper
`scripts/llamacpp/run-llama-server.sh`, which reads the rendered `LLAMACPP_*` env and builds the full
`llama-server -m /models/<gguf> -c 131072 -ngl -1 …` argv (exactly what V1's compose does at
`ordo-ai-stack/docker-compose.yml:51,80-81`). Missing that entrypoint + the two bind mounts
(`${BASE_PATH}/models/gguf:/models:ro`, `${BASE_PATH}/scripts/llamacpp:/llamacpp-scripts:ro`), the image
fell through to its default entrypoint → model-less router mode. The `LLAMACPP_*` env was rendered
correctly but nothing consumed it.

**Fix (this commit):** `ordo/compose.py` now emits the wrapper `entrypoint` + both bind mounts for
`llamacpp`, mirroring V1 exactly. Weights + wrapper are shared-by-path from the V1 tree via `${BASE_PATH}`
(already in `.env`) — no copy. Re-rendered `out/`, **119/119 tests pass**, preflight still **GO**.

### State after this attempt
- **V1 = intact rollback asset:** 24 running (== pre-flight), volumes/images untouched; owns tailnet `:443`.
- **V2 = stopped-intact:** 6 containers `exited` (not removed), named volumes `ordo-v2_grafana-data`
  + `ordo-v2_prometheus-data` intact — ready for a clean retry from `out/` after this fix.
- **Retry procedure is unchanged** — re-run this runbook from step 1. The one delta: the Hermes brain was
  already delta-synced at 14:14 and V1's hermes kept writing after rollback, so the retry's step-1
  delta-sync will re-mirror the newer V1 state (correct by design).

---

## Phase-5 EXECUTED — attempt #2: ROLLED BACK (2026-07-09), then second render defect root-cause fixed

**Outcome: clean rollback again. The attempt-1 llamacpp fix WORKED (llamacpp loaded the model, served
a real HTTP-200 chat completion — the defect that killed attempt 1 is gone). A SECOND, independent
render-engine defect surfaced: the `agent` (Hermes) container crash-looped on `hermes --help`. Found
live, FIXED upstream in this commit — the next attempt should pass.**

### Timeline (UTC)
- `14:27:53` — stopped V1 hermes-gateway + hermes-dashboard; robocopy `/MIR` delta-sync of the brain
  into `C:\dev\ordo-v2\data\hermes` (exit 1 = success; 15 files updated, 0 failed, 0 extras);
  cleared stale locks in the V2 copy only (gateway.lock, auth.lock, shell-hooks-allowlist.json.lock;
  gateway.pid already absent).
- `14:28:09` — **V1 full stop** (`docker compose -p ordo-ai-stack stop`). 0 V1 running; 5090 VRAM → 4 MiB.
- `14:28:30` — V2 core `up -d` (RECREATE from re-rendered out/): 6 containers. llamacpp **Recreated** with
  the fixed entrypoint `/bin/sh /llamacpp-scripts/run-llama-server.sh` + both binds (`/models:ro`,
  `/llamacpp-scripts:ro`) — verified by `docker inspect`.
- `14:29–14:30` — **attempt-1 fix CONFIRMED WORKING:** llamacpp wrapper built the full argv
  (`--model /models/Huihui-Qwen3.6-27B-…Q6_K.gguf --ctx-size 131072 --n-gpu-layers -1 …`), model
  loaded, `/health → {"status":"ok"}`, 5090 climbed to ~24.7 GB. Chat proof via model-gateway:
  `POST local-chat → HTTP 200`, `model:"local-chat"`, `system_fingerprint:"b1-86b9470"` (patched build).
- `~14:31` — **defect #2 detected:** `ordo-v2-agent-1` in a restart loop, printing `hermes --help`
  usage then exiting 0. Root cause below.
- `14:32:57` — **ROLLBACK:** `docker compose -p ordo-v2 stop` (edge never came up → `:443` never contended).
- `14:33:07` — `docker compose -p ordo-ai-stack start`. (Same transient `network … not found` on start
  as attempt 1 — a stale ref as `ordo-v2-net` was torn down same session; healed, no container left down;
  all 24 came back up.)
- `~14:35` — V1 llamacpp `model loaded` → healthy, 5090 back to **29711 MiB** (normal resident).
  V1 chat proof: `POST local-chat` → **HTTP 200**, `system_fingerprint:"b1-86b9470"`. All **24** V1
  containers healthy again (gpu-exporter `unhealthy` = pre-existing driver-581.80, not a regression);
  hermes-gateway loaded its full MCP catalog + Discord gateway reconnected.

**Total production downtime ≈ 7 min** (14:28:09 stop → ~14:35 V1 serving again).

### Root cause (the real fix, not a bandaid)
The `agent-hermes` image's default `CMD` is `["hermes","--help"]` (prints usage + exits 0). V1's compose
overrides this with `command: ["hermes","gateway"]` (`ordo-ai-stack/docker-compose.yml:1131`) to launch
the persistent messaging gateway. **V2's render engine emitted the agent service with NO `command`,** so
the image default won and the container restart-looped on `hermes --help`. The agent is swappable, so the
launch command belongs in the agent manifest (data-driven), not hardcoded.

**Fix (this commit):**
- `agents/hermes/agent.yaml` — declares `command: [hermes, gateway]`.
- `ordo/agents.py` — `Agent` gains a `command` field (parsed from the manifest; empty → omitted).
- `ordo/render.py` — threads `agent.command` into the render context (`hermes.agent_command`).
- `ordo/compose.py` — `render_compose` emits `command:` on the agent service when the manifest declares
  one (empty → omitted, so a self-starting agent image is unaffected). Mirrors V1 exactly.
- `tests/test_agents.py` — 3 regression guards: manifest declares `hermes gateway`; rendered agent
  service carries `command: [hermes, gateway]`; an agent with no manifest command omits `command`.

Re-rendered `out/`, **122/122 tests pass** (was 119; +3 new), preflight still **GO**. The attempt-1
llamacpp entrypoint+binds fix is preserved in the re-render (verified).

### State after this attempt
- **V1 = intact rollback asset:** 24 running (== pre-flight), volumes/images untouched; owns tailnet `:443`.
- **V2 = stopped-intact:** 6 containers `exited` (not removed), named volumes intact — ready for a clean
  retry from the newly re-rendered `out/`.
- **Retry procedure unchanged** — re-run from step 1; step-1 delta-sync will re-mirror the newer V1 state.
  With BOTH render defects (llamacpp launch + agent launch) now fixed and test-guarded, attempt #3 should
  bring the full core up healthy.

---

## Phase-5 EXECUTED — attempt #3: SUCCESS — V2 IS NOW PRODUCTION (2026-07-09)

**Outcome: the cutover LANDED. All 23 V2 services are up; core proven with a real HTTP-200 chat
completion (fingerprint `b1-86b9470`), a non-empty MCP tool catalog + a live tool→qdrant data
round-trip, the agent's Discord gateway connected with cron/skills/memories loaded, voice pinned to
the 1070, comfyui with all 27 custom nodes, edge SSO redirecting to Google, and the root-cause cure
demonstrated (llamacpp ran restarts=0 the whole time comfyui came up — no reactive guardian). Two
NEW live-only defects surfaced and were FIXED UPSTREAM mid-flip (no rollback needed — they blocked
only the agent's start-gate and the scheduler's GPU visibility, both recoverable in place).**

### Timeline (UTC)
- `15:00:54` — stopped V1 hermes-gateway + hermes-dashboard; robocopy `/MIR` delta-sync of the brain
  (exit 1 = success; 15 files updated, 0 failed, 0 extras); cleared stale locks in the V2 copy only
  (gateway.lock, auth.lock, shell-hooks-allowlist.json.lock; gateway.pid already absent). Confirmed
  `history_backfill: false` survived the sync.
- `15:01:14` — **V1 full stop** (`docker compose -p ordo-ai-stack stop`). 0 V1 running; 5090 → 4 MiB.
- `15:01:33` — V2 core `up -d` (recreate). **Defect #5 detected immediately:** the agent's audit-added
  `depends_on: dashboard: service_healthy` gate was **unsatisfiable** — Compose refused with
  "dashboard has no healthcheck configured". Root cause below. Core minus-agent came up; the fix was a
  small data-driven render change, so fixed-in-place rather than rolled back (well inside the window).
- `15:04:58` — V2 core RE-UP after fix #5. All three `service_healthy` gates (model-gateway,
  mcp-gateway, dashboard) went **Healthy**, agent **Started + healthy**. llamacpp wrapper built the
  full argv, model loaded, `/health {"status":"ok"}`, 5090 → ~28.6 GB. **Chat proof:** `POST local-chat`
  → **HTTP 200**, `model:"local-chat"`, `system_fingerprint:"b1-86b9470"`, content `"PONG"`.
- `~15:10` — **Defect #6 detected** via ops-controller `/status`: scheduler reported `total_vram_gb: 0`
  (CPU-only) and dropped comfyui/voice/worker as "not available". Root cause below (missing utility-GPU
  visibility). Fixed-in-place; `15:11:35` ops-controller recreated → `nvidia-smi` now injected, `/status`
  reports `RTX 5090 32GB`, `total_vram_gb: 31.8`, and all GPU plugins re-enabled.
- `15:12:09–15:12:55` — profile groups up one at a time (rag → webui/automation/search/codebase-memory/
  hermes-ui/monitoring/media/voice → **edge LAST**). No crash-loops (all RestartCount ≤ 1). Voice pinned
  to the 1070 uuid, comfyui/llamacpp-embed to the 5090 uuid (verified by `docker inspect`).
- `~15:13` — edge live: caddy bound tailnet `100.85.139.89:443` (both PortBindings AND NetworkSettings
  populated — no silent-loss), `https://ultracam.tail63bdfc.ts.net/` → **302 /oauth2/start → 302
  accounts.google.com** (Google SSO enforced).

**Total production downtime for the CORE chat path ≈ 3.75 min** (15:01:14 V1 stop → 15:04:58 V2 core
re-up healthy + serving 200). Full stack (all profiles + edge) settled by ~15:13.

### Root causes (real fixes, not bandaids) — both fixed upstream this commit

**Defect #5 — dashboard + model-gateway had no healthcheck, so the agent's `service_healthy` gates
were unsatisfiable.** The 5.5 audit (G5) correctly ported the agent's `depends_on: {dashboard,
model-gateway, mcp-gateway}: service_healthy` conditions, but ONLY `mcp-gateway` rendered a
healthcheck. V1's dashboard (`/api/health` via python3) and model-gateway (`/v1/models` w/ bearer) BOTH
declare healthchecks; V2 dropped them — the same "V2 silently omits a piece of V1 per-service config"
class the audit targeted, just on services whose OWN healthcheck dimension the audit hadn't checked
(both were logged as INTENTIONAL-DIFF "V2-native", which skipped their healthcheck audit).
*Fix:* `ordo/compose.py` now renders `_dashboard()` (curl `/api/health` — the V2 image ships curl, not
V1's python3) and `_model_gateway()` (V1's exact python3 `/v1/models` bearer probe — that image ships
python3). Regression test `test_service_healthy_depends_targets_all_have_healthchecks` asserts EVERY
service the agent gates on via `service_healthy` renders a healthcheck (guards the whole class).

**Defect #6 — ops-controller had no GPU visibility, so the scheduler saw 0 VRAM.** V2's scheduler is
the advertised replacement for V1's reactive guardian; its core job is VRAM-fit co-run admission. It
detects VRAM by shelling to `nvidia-smi` inside its container (`hardware._detect_gpus`). V1's
ops-controller reserves a GPU with `caps=[[utility]]` (the NVIDIA toolkit injects `nvidia-smi`/NVML
without reserving compute); V2 rendered NO DeviceRequest at all → no `nvidia-smi` → CPU-only →
`total_vram_gb: 0`, and comfyui/voice/song-gen/worker dropped as "not available". The audit's
ops-controller INTENTIONAL-DIFF addressed its *guardian mounts* (correctly absent) but never checked
the utility-GPU dimension. *Fix:* `_ops_controller()` now emits a `utility`-capability reservation
(`count: all`, read-only — reads both cards, pins compute to neither) + `NVIDIA_DRIVER_CAPABILITIES=
utility`. Regression test `test_ops_controller_has_utility_gpu_visibility` guards it. After the fix
`/status` reports `RTX 5090 32GB · total_vram_gb 31.8` and all GPU plugins enabled.

**Defect #7 — comfyui rendered `PYTORCH_CUDA_ALLOC_CONF: ""` (empty), crash-looping torch.** After
the profile groups came up, comfyui restart-looped (exit 0, RestartCount climbing). Log:
`ValueError: Unrecognized key ',' in CUDA allocator config` at `torch._C._cuda_init()` →
`execution.py` crashes the server every boot. Root cause: the comfyui plugin manifest hardcoded
`PYTORCH_CUDA_ALLOC_CONF: ""`; an EMPTY-but-present env var is WORSE than omitting it — torch's
allocator parser rejects it. V1 sets the real value
`expandable_segments:True,pinned_use_cuda_host_register:True` (overrides/compute.yml). The GPU itself
was fine (nvidia-smi -L inside the container listed both cards; the 5090 uuid pin was correct) — purely
a bad config string. *Fix:* `plugins/comfyui/plugin.yaml` defaults `PYTORCH_CUDA_ALLOC_CONF` to V1's
value via `${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True,pinned_use_cuda_host_register:True}`
(operator-overridable, never empty). Regression test `test_comfyui_alloc_conf_never_empty` guards it;
`docker compose config` confirms the resolved value matches V1.

Re-rendered `out/` after each fix; **137/137 tests pass** (was 134; +3 new class guards), ruff clean,
preflight **GO** each time. The attempt-1 llamacpp fix + attempt-2 agent-command fix are preserved in
the re-render (verified by `docker inspect` on the running containers). Defects #5/#6 blocked only the
agent start-gate + scheduler GPU visibility (recoverable in place, no rollback); #7 affected only the
media plugin (comfyui) — core + all other services were already green when it surfaced.

### Validations (with evidence)
- **(a) Chat:** `POST local-chat` → HTTP 200, `system_fingerprint b1-86b9470`, content `"PONG"`/`"READY"`.
- **(b) Agent:** Discord gateway `state: connected` (gateway_state.json); `hermes cron list` → 3+ active
  jobs (Daily AI News, GitHub Monitor, social-relay-dialogue-reel `68681701c991`) with FUTURE next-runs
  (no stale re-run; history_backfill:false held); full skill tree + SOUL.md/memories present;
  `active_agents: 0` (not executing backfilled work). No permission errors on `/home/hermes/.hermes`.
- **(c) MCP:** gateway spawned qdrant-rag + searxng over docker.sock, listed **6 tools** (searxng 4 +
  qdrant-rag 2 — full, not docs-only). Live round-trip: `qdrant_status` → real qdrant, `points_count: 1`.
- **(d) ops-controller `/status`:** GPU idle, 31.8 GB free, scheduler present, no guardian container.
  dashboard `/api/health` → 200.
- **(e) Voice:** stt healthy (`/v1/models` 200 via python3), tts voices list served (af_bella default);
  both `device_ids` = 1070 uuid `GPU-20fac13a`.
- **(f) Monitoring:** prometheus active targets all `up` (llamacpp:8080/metrics, gpu-exporter:9835,
  prometheus). grafana `/api/health` → 200.
- **(g) ComfyUI:** 27 custom nodes present (ACE-Step, GGUF, GeometryPack, LTXVideo, TRELLIS2, WhisperX,
  KokoroTTS…); pinned to 5090 uuid. Crash-looped on the empty-alloc-conf defect #7, fixed in place;
  torch CUDA init clean after the fix (nvidia-smi saw both cards throughout).
- **(h) Edge:** caddy `:443` bound to tailnet IP; `/` → 302 `/oauth2/start` → 302 Google SSO.
- **(i) qdrant/open-webui/n8n/searxng/codebase-memory-ui:** all healthy (200 on their probes).
- **(j) ROOT-CAUSE PROOF:** llamacpp `RestartCount=0`, running since 15:05:02 through comfyui's entire
  boot — V1's guardian would have evicted it; V2 has no guardian and llamacpp never blinked. Co-run chat
  returned 200 WHILE comfyui was up.

### State after this attempt
- **V2 = PRODUCTION:** 23 running; all healthy except gpu-exporter `unhealthy` (pre-existing
  driver-581.80 cosmetic — prometheus scrapes it `up`) and comfyui warming its 27 nodes on first boot.
  Owns tailnet `:443`.
- **V1 = stopped-intact (rollback asset):** 0 running, 29 containers `exited` (not removed), 6 named
  volumes intact. Instant rollback remains `stop` V2 / `start` V1 until V2 is trusted (§5/§6).

## Post-cutover EXECUTED — V1 dashboard reinstated (2026-07-09)

The parity mapping had swapped the operator's feature-rich V1 dashboard for the minimal V2-native
SPA. Reinstated data-driven (see AUDIT.md "Dashboard reinstatement" / G8). UI + control-plane only;
no GPU-bound service intentionally recreated.

### What changed
- Source: `dashboard: v1-parity` in `ordo.yaml`; new `ordo/dashboards.py` + `dashboards/{v2-native,
  v1-parity}/dashboard.yaml` (registry mirrors the agent pattern), threaded render→compose. +12 tests
  → **151/151 pass**, ruff clean, `docker compose config` exit 0.
- Images (NEW, project-namespaced, built from V1 context — V1 images untouched):
  `ordo-v2/dashboard-v1:latest` (V1 dashboard **unchanged** — same-origin frontend, backend URL is
  runtime env) + `ordo-v2/ops-api:latest` (V1 ops-controller + guardian/mutation kill-switches).
- Naming: dashboard keeps name `dashboard` (Caddy `/dash/*` unchanged) with
  `OPS_CONTROLLER_URL=http://ops-api:9000`; V2 scheduler stays `ops-controller`.

### Timeline / incident (UTC)
- `~15:45` — **first apply MISTAKE:** re-rendered `out/` inside a NON-GPU container, so `hardware:
  auto` → CPU → llamacpp lost its 5090 pin (`GPU_LAYERS=0`, `count: all`). `docker compose up -d
  ops-api dashboard` (without `--no-deps`) recreated llamacpp → it crash-looped (`libcuda.so.1
  missing`) and then loaded on the wrong GPU (1070). Caught immediately via `docker inspect`
  RestartCount + `nvidia-smi` (5090 idle at 499 MiB).
- **Root-cause fix (not a bandaid):** the render MUST run where it can see the GPUs. Re-rendered `out/`
  in a `--gpus all` CUDA container (real `detect()` → 5090+1070 uuids) → llamacpp correctly pinned to
  the 5090 uuid (`device_ids` + `CUDA_VISIBLE_DEVICES`). Recreated llamacpp ONCE to restore it →
  loaded on the 5090 (VRAM 29 GB, `/health` 200, RestartCount 0). All subsequent applies used
  `--no-deps` so no GPU service was touched again.
- Recreated `dashboard` + created `ops-api` with `--no-deps`. dashboard first crash-looped on
  `PermissionError: /app/data` (V1 `DASHBOARD_DATA_PATH` defaulted under non-writable `/app`); fixed
  in the manifest (`DASHBOARD_DATA_PATH=/data/dashboard` + a `${DATA_PATH}/dashboard` mount),
  re-rendered on GPU, recreated dashboard → **healthy**.

### Validations (with evidence)
- **Dashboard `/api/health`** → 200; V1 SPA serves (195 KB single-file UI, `/api/*` calls present).
- **`/api/llm/*`** → 200 listing the REAL GGUF dir (Huihui-Qwen3.6-27B Q6 22 GB, Qwen3.6-27B Q4,
  mmprojs, nomic-embed) — GGUF management works.
- **`/api/model-config`** (ops-api flag-card system) → 200, **19 flags**, 2 models on disk;
  **`/api/services`** → 200 real inventory; **`/mcp/containers`** → 200 (guard-scoped).
- **Guardian panel** → `/guardian/status` = `{"enabled":false,"state":"disabled"}` (inert, graceful).
- **Compose kill-switch** → `POST /compose/restart` = **501** "use the ordo-v2 control plane".
- **Caddy edge** → `dashboard:8080/api/health` and `grafana:3000/api/health` both 200 from the caddy
  container; `/dash/*` route + SSO forward_auth chain unchanged.
- **V2 scheduler still owns GPU:** `ops-controller /status` → 200, model `huihui-qwen3.6-27b-q6`,
  `total_vram 31.8`. **llamacpp UNTOUCHED** by the dashboard/ops-api applies: `RestartCount=0`,
  running since the correct-pin recreate, on the 5090.
- **V1 untouched:** 0 running, 29 exited, original image IDs (`6760cc087b9b` / `17921b0f885b`) intact.

## Gap-fix: per-service recreate controls (were 501 stubs → now WIRED-SAFE) — EXECUTED, live

The reinstated V1 dashboard's recreate buttons proxied to `/services/{llamacpp,open-webui}/recreate`,
which the 501 kill-switch stubbed → the buttons were broken. Wired them to a SAFE, replay-only path.

### Change (data-driven, committed; no hand-edit of `out/`)
- **`docker/ops-api/compose_recreate.py`** (new, pure/dep-free, unit-tested): `build_recreate_cmd` +
  `discover_profiles`. The single command it emits is the ONLY thing that shells docker-compose for a
  button recreate:
  `docker-compose --project-name ordo-v2 --project-directory /workspace -f /workspace/docker-compose.yml
  --profile <all> --env-file /workspace/.env --env-file /workspace/secrets.env up -d --no-deps
  --force-recreate <svc>` — **BOTH env files**, **`--no-deps`**, **no re-render**.
- **`docker/ops-api/main.py`**: new `OPS_SERVICE_RECREATE_ENABLED` gate (default OFF) rewires
  `_recreate_service` + `/services/{id}/recreate` to the pure builder; whole-stack `/compose/*` stays
  gated on `OPS_COMPOSE_MUTATIONS_ENABLED` → **501**. Profiles discovered from the compose file at
  runtime (drift-free — sourced from the artifact being replayed). Timeout 120→180s (22GB reload).
- **`docker/ops-api/Dockerfile`**: COPY the new module.
- **`dashboards/v1-parity/dashboard.yaml`**: ops-api now bind-mounts the rendered `out/` tree RW at
  `/workspace` (`./:/workspace:rw`, mirrors ops-controller's `./:/config`) so `/env/set` writes and the
  recreate replay share ONE `.env` + `secrets.env`; sets `OPS_SERVICE_RECREATE_ENABLED=1`; moves the
  gguf mount to `/gguf-models` so it doesn't nest under the RW out/ mount.

### Re-render (manifest changed) — pin-identical, verified
Re-rendered `out/` in a `--gpus all` CUDA container (real `detect()` → 5090+1070 uuids). `diff` of the
new vs live `docker-compose.yml` = **ONLY the 3 intended ops-api lines** (`OPS_SERVICE_RECREATE_ENABLED`,
`LLAMACPP_MODELS_DIR`, the two mount lines). **llamacpp block byte-identical** — pin stays
`GPU-97fe65ee-5e2d-8c9b-32d0-362f510ceb96`. `docker compose config` (both env files) → exit 0. Only
`ops-api` rebuilt + recreated (`--no-deps`).

### Bug caught + fixed mid-flight (honest)
1. First recreate crash-looped: `FileNotFoundError: /app/compose_recreate.py` — Dockerfile COPY missed
   the new module. Fixed (added to COPY), rebuilt.
2. First open-webui recreate returned `webui_error: "no such service: qdrant"` — open-webui
   `depends_on: qdrant`, and qdrant is behind the `rag` profile; a per-service `up` without the
   profile can't resolve the reference. **Root-cause fix (not a bandaid):** discover + pass ALL
   profiles the stack runs with (from the artifact), so profiled `depends_on` resolves; `--no-deps`
   still scopes the recreate to the one service. Re-verified.

### Validations (with evidence)
- **(a) open-webui recreate via the dashboard route** (`POST /api/config/default-model`, DASHBOARD_AUTH
  bearer) → `webui_recreated: true`. New container id `d45c049f…` (was `9ad0848f…`), **healthy**,
  RestartCount 0. **Secrets intact** (2026-06-26 regression check): `OAUTH2_PROXY_CLIENT_SECRET=GOCSPX-7…`,
  `OAUTH2_PROXY_COOKIE_SECRET=OvX7lUy1…`, `SEARXNG_SECRET=09e4f80a…`, `OPS_CONTROLLER_TOKEN=81d8a9c4…`
  all non-empty.
- **(b) llamacpp recreate via the Model Control route** (`POST /api/llamacpp/switch`, same model,
  idempotent). Pre-checks: scheduler `gpu.state=idle`, `running:[]`; no in-flight hermes work. Result:
  new container id `5d2d0510…` (was `258a61c9…`), image `qwen36-swa-86b9470`, **pin still
  `GPU-97fe65ee-…`** (CUDA_VISIBLE_DEVICES + device_ids), model resident **29070 MiB on the 5090**
  (1070 untouched), RestartCount 0/stable, chat via model-gateway → **200, `fp=b1-86b9470`**,
  `reasoning_content` populated (empty `content` is Qwen3.6 reasoning behavior, identical pre/post).
- **(c) whole-stack stays disabled:** `/compose/{up,down,restart}` all → **501**; guardian
  `{"enabled":false,"state":"disabled"}`.
- **Scope:** only `ops-api` + `open-webui` + `llamacpp` recreated (rest "Up about an hour"); **V1
  untouched** (0 running, 29 exited).
- **Offline:** ruff clean (ordo + tests + the touched ops-api modules) + **166 passed** (was 151;
  +15: 11 in `test_compose_recreate.py`, +4 render/gate guards in `test_dashboards.py`).

## Gap-fix: ops-api GPU visibility — dashboard GPU widgets went blank ("No GPUs returned") — EXECUTED, live

The reinstated V1 dashboard's GPU/registry widgets showed *"No GPUs returned from registry. WSL
passthrough may be down."* — **WSL was fine** (llamacpp served off the 5090). Two-fold root cause:

1. **ops-api had NO GPU access.** It IS a copy of V1's ops-controller and enumerates GPUs by shelling
   to `nvidia-smi`, which the NVIDIA runtime injects only when the service reserves a GPU with the
   **`utility`** capability. The reinstatement dropped it (parity oversight): `docker inspect
   ordo-v2-ops-api-1` → `DeviceRequests=null`, no `nvidia-smi` in the container → zero GPUs enumerated
   → the `/api/registry/gpus` route returned empty → the widget's "No GPUs" branch fired. V1 ground
   truth (`docker inspect ordo-ai-stack-ops-controller-1`): `caps=[[utility]]`, and (verified via
   `.Config.Env`) NO `NVIDIA_*` env vars — the capability alone triggers the injection.
2. **Downstream: nulled registry.** ops-api's startup reconcile, running blind, seeded the staged
   registry with `gpu_uuid: null` everywhere (`updated_by: reconcile`). V1's original registry was
   intact at `C:\dev\ordo-ai-stack\data\ops-controller\model-registry.json`.

### Change (data-driven, committed; no hand-edit of `out/`)
- **`ordo/dashboards.py`**: new `gpu_capabilities` field on `DashboardBackend` + a `_gpu_caps` parser
  accepting the `gpu: <cap>` shorthand OR `gpu_capabilities: [utility]` list.
- **`ordo/compose.py`**: refactored `_utility_gpu_reservation` onto a shared
  `_capability_gpu_reservation(caps)`; `_dashboard_backend` now renders the all-GPU (`count: all`)
  reservation when the backend declares capabilities.
- **`ordo/render.py`**: flows `gpu_capabilities` into the backend dict.
- **`dashboards/v1-parity/dashboard.yaml`**: ops-api backend now declares `gpu: utility` (mirrors V1's
  ops-controller `caps=[[utility]]` exactly; NO `NVIDIA_*` env, matching V1).
- **Regression tests (+4, → 170):** utility reservation renders; a plain `gpu:true` service still
  reserves the compute `gpu` cap (not utility); a no-GPU backend gets no reservation; the real
  `v1-parity` manifest end-to-end gives ops-api the utility reservation.

### Re-render (manifest changed) — llamacpp byte-identical, verified
Re-rendered `out/` in a `--gpus all` CUDA container (`nvidia/cuda:12.4.1-base` → real `detect()`
listed 5090 `GPU-97fe65ee-…` + 1070 `GPU-20fac13a-…`). `diff` new vs live `docker-compose.yml` =
**ONLY the 8-line ops-api `deploy` block** (`{driver: nvidia, count: all, capabilities: [utility]}`).
**llamacpp block byte-identical** (raw-text SHA `856ef157…` both sides; pin stays `GPU-97fe65ee-…`).
`.env` NOT overwritten (live has runtime `/env/set` keys `DEFAULT_MODEL`/`OPEN_WEBUI_DEFAULT_MODEL`
the render doesn't emit — only `docker-compose.yml` was applied; live `.env`/`secrets.env` preserved).
`docker compose config` (both env files, all profiles) → exit 0. Applied `up -d --no-deps --force-recreate ops-api`.

### Registry restore (clobber-safe)
ops-api STOPPED → nulled registry backed up (`model-registry.json.nulled.bak`) → V1's intact registry
copied over → ops-api started (now GPU-enabled). Reconcile is **seed-only** (`if mid in existing:
continue`) so it preserved every restored record; post-boot re-read confirms real uuids
(chat/embed/comfyui → 5090, voice → 1070), config blocks populated, provenance `model-config`/`dashboard`.
No background writer nulls the registry (only startup-reconcile + explicit HTTP verbs mutate it).

### Validations (with evidence)
- **`docker inspect ordo-v2-ops-api-1`** → `DeviceRequests=[["utility"]]`; **`nvidia-smi -L`** inside →
  lists BOTH cards (5090 + 1070); healthy, RestartCount 0.
- **`/api/registry/gpus`** (DASHBOARD_AUTH bearer, the exact route the GPU widget calls) → **BOTH GPUs**:
  5090 (31.8 GB, 28.4 used, models `comfyui`/`local-chat`/`local-embed`) + 1070 (8.0 GB, 2.5 used,
  `voice-stt`/`voice-tts`). `data.gpus` non-empty → the "No GPUs returned" condition is GONE.
- **`/api/registry/models`** → all 5 models with real GPU assignments.
- **Scheduler `ops-controller /status`** → 200, model `huihui-qwen3.6-27b-q6`, unaffected.
- **llamacpp UNTOUCHED:** container id `5d2d0510b871…` + RestartCount 0 — identical to pre-work.
- **Scope:** ONLY `ops-api` recreated (`--no-deps`); **V1 untouched** (0 running, 29 exited).
- **Offline:** ruff clean + **170 passed** (was 166; +4 GPU-reservation guards in `test_compose.py`).
