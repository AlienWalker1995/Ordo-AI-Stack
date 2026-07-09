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
  `ordo.yaml` (for in-place re-render), `secrets.env` (gitignored, real values), `auth/`, `monitoring/`.
- V2 named volumes `ordo-v2_grafana-data` / `ordo-v2_prometheus-data` pre-populated from V1 history.

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
