# Cutover runbook — migrating from the previous stack to Ordo

> _Archival record of the one-time build+cutover that produced today's stack. There is no ongoing "V1/V2" split — the stack is simply **Ordo**. Here, "V1" = the previous stack and "V2" = the current substrate under `v2/`._

> **STATUS: EXECUTED — 2026-07-09. This cutover is DONE.** Ordo v2 is now the production stack,
> running entirely from `C:\dev\ordo-ai-stack` on `main` (compose project `ordo-v2`, 24 services).
> The flip succeeded at ~3.75-min core downtime (after two clean ~7-min rollbacks that each turned a
> live defect into a test-guarded fix), then a consolidation re-homed the runtime to the primary
> checkout and merged to `main`. The old V1 stack is stopped-intact (containers removed, 6 volumes +
> all images KEPT for reconstitution). See **[`FLIP.md`](./FLIP.md)** for the executed flip +
> consolidation record with evidence, and the **"CONSOLIDATION EXECUTED"** section at the bottom of
> this file. The procedure below is retained as the runbook of record for how it was performed (and
> how a future rebuild-and-flip would go); read it as a completed runbook, not a pending action.

This was the **operator's** procedure. Nothing here ran automatically: the substrate was built and
validated in isolation, and bringing v2 up, validating GPU work, and retiring the old stack touched
the live containers and the 5090.

The design was a **big-bang rebuild with an atomic cutover** (agreed in the interrogation): v2 ran
its own isolated compose project beside the live stack, parity was validated in full, then it flipped.
The old stack stayed intact for instant rollback throughout.

---

## 0. Preconditions (once)

- *(as performed)* v2 was developed at `C:\dev\ordo-v2` on branch `arch/v2-substrate` — a **separate
  worktree**, beside the untouched live stack at `C:\dev\ordo-ai-stack`. Post-consolidation this is
  no longer true: the retired worktree is gone and everything now runs from `C:\dev\ordo-ai-stack`
  on `main` (see the STATUS banner + "CONSOLIDATION EXECUTED").
- A **current** personal backup exists (Hermes `data/` + personal automation), committed. Verify the
  restore actually works into v2 before you flip — a backup you haven't restored is a guess.

> **This box:** the source is already authored at `v2/ordo.yaml` (pinned model
> `huihui-qwen3.6-27b-q6`, the full 5090+1070 parity plugin set, and a `site:` block with the real
> host paths + edge identity). The exact, verified Phase-5 flip commands for this box live in
> **[`FLIP.md`](./FLIP.md)** — use that; the sections below are the generic procedure.

## 1. Author the source

```
cd C:\dev\ordo-v2\v2
python -m ordo.cli setup --yes        # detect hardware -> write ordo.yaml (or hand-write it)
python -m ordo.cli detect             # sanity: tier / model / ctx / plugins it will pick
```

Set host paths + edge identity in the source's `site:` block (see `ordo.example.yaml`): `DATA_PATH`,
`BASE_PATH`, `CODE_ROOT`, `CADDY_*`. These render into `.env` so plugin `${VAR}` binds resolve to a
deterministic absolute path instead of `./data` under `out/`. They are host config, not secrets.

## 2. Build the v2 images

Project images are the substrate's own (buildable-not-pullable); the rest (qdrant, n8n, open-webui,
searxng, grafana, prometheus, gpu-exporter, oauth2-proxy, caddy, stt/tts, upstream llama.cpp) are
pulled on first `up`. `ordo preflight` lists any missing build-first image — build **all** of these
before the flip (build only the ones your enabled profiles need):

> **Dashboard note (as shipped):** production reinstated the **V1-parity** dashboard, not the
> minimal V2-native SPA. That means two images instead of `ordo-v2/dashboard:latest`: the V1 SPA
> reused unchanged as `ordo-v2/dashboard-v1:latest` (service `dashboard`) and its backend
> `ordo-v2/ops-api:latest` (service `ops-api`, a copy of V1's ops-controller with guardian/watchdogs
> OFF). See `docker/ops-api/README.md` and AUDIT.md "Dashboard reinstatement". The `ordo serve`
> scheduler control plane below (`ops-controller`) is a *separate* service.

```
# --- V2-native core control plane (the ordo serve scheduler) ---
docker build -f docker/ops-controller.Dockerfile -t ordo-v2/ops-controller:latest .

# --- dashboard: production uses the reinstated V1-parity pair (see note above) ---
docker build -t ordo-v2/ops-api:latest       docker/ops-api
docker build -t ordo-v2/dashboard-v1:latest  C:/dev/ordo-ai-stack/dashboard

# --- config-wrapper core images (V1 parity: local-chat alias, mcp reload wrapper) ---
docker build -t ordo-v2/model-gateway:latest  docker/model-gateway
docker build -t ordo-v2/mcp-gateway:latest    docker/mcp-gateway

# --- patched llama.cpp (only if the chosen model pins it via catalog backend_image) ---
docker build -t ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470 docker/llamacpp-patched

# --- ported service plugins (build the ones whose profiles you enable) ---
docker build -t ordo-v2/rag-ingestion:latest      C:/dev/ordo-ai-stack/rag-ingestion        # profile rag
docker build -f C:/dev/ordo-ai-stack/worker/Dockerfile -t ordo-v2/worker:latest C:/dev/ordo-ai-stack  # profile media
docker build -t ordo-v2/codebase-memory-ui:latest C:/dev/ordo-ai-stack/codebase-memory-ui   # profile codebase-memory

# --- MCP (kind=mcp) — the qdrant-rag MCP is a project buildable image ---
docker build -t ordo-v2/qdrant-rag-mcp:latest     C:/dev/ordo-ai-stack/qdrant-rag-mcp

# --- operator-specific: the agent (wraps your Hermes data/) — also serves hermes-dashboard ---
docker build -t ordo-v2/agent-hermes:latest       C:/dev/ordo-ai-stack/hermes               # profile hermes-ui
# comfyui / voice images are your operator-specific media/voice builds.
```

Each `docker/<name>/README.md` documents the exact context + files. Contexts referenced at their
V1 path are the single source of truth (referenced, not duplicated, so they can't drift).

### Secrets

`ordo render` writes `out/secrets.env.example` — the secret KEYS the enabled stack needs (values
empty). Copy it to `out/secrets.env`, fill real values (SOPS-decrypt or hand-set), and **never
commit it**. The rendered compose reads `secrets.env` as a second env_file (`required: false`) for
services that need secrets; `.env` stays derived-config only. Verify with
`ordo preflight --secrets out/secrets.env` (a non-blocking check flags any missing key).

## 3. Render + preflight (the GO/NO-GO gate)

```
python -m ordo.cli render --out out                     # writes out/{.env,docker-compose.yml,…}
python -m ordo.cli preflight --ref C:\dev\ordo-ai-stack\.env
```

`preflight` is **read-only**. It renders the target and checks: ctx consistency (drift gate),
model + MCP checksums, GPU present for enabled media/voice plugins, **parity vs the live `.env`**
(merge gate a), and that every required image is built/cached. It prints `GO` or `NO-GO` and exits
non-zero on NO-GO. Do not proceed on a NO-GO — resolve the `[!!]` lines first.

## 4. Bring v2 up **beside** the live stack

v2 uses its own compose project (`ordo-v2`) and network, and publishes **no host ports** on core
services, so it cannot collide with the running stack. Both can be resident; only the GPU is shared,
so expect to keep heavy media off until you flip.

```
cd out
docker compose -p ordo-v2 up -d                         # core only
docker compose -p ordo-v2 --profile media up -d         # add ComfyUI when you want to test media
```

The rendered compose publishes **no host ports** (so it can't clash with the live stack). To open
the localhost dashboard while testing, map its port ad-hoc without editing the render:

```
docker compose -p ordo-v2 run --rm -p 8080:8080 dashboard   # then browse http://localhost:8080
# (or add a compose override with the port; the dashboard proxies /api/* to the control plane)
```

## 5. Validate parity on the running v2 (before retiring anything)

- **Restore the personal backup into v2** (Hermes `data/`, automation) and confirm crons/skills load.
- Exercise the real paths: a chat turn, an MCP tool call, a media render, a scheduler co-run
  (`POST /jobs` a media + a chat and confirm they co-run via `GET /status`), a model switch
  (`POST /model-config`) and confirm `.env` regenerates with ctx moving in lockstep.
- Let it sit for the agreed N-day testbed window and watch for the old pains (drift, VRAM deadlock).

## 6. Atomic flip

Only after step 5 is green:

```
# stop the OLD stack (this is the one moment the old containers go down)
cd C:\dev\ordo-ai-stack && docker compose down
# v2 is already up; if the old stack owned host ports/edge (Caddy), bring v2's edge up now
```

Keep the old stack's volumes/images **intact** — do not prune. That is your rollback.

## 7. Rollback (if anything is wrong)

```
cd out && docker compose -p ordo-v2 down                # stop v2
cd C:\dev\ordo-ai-stack && docker compose up -d         # the old stack returns, unchanged
```

## 8. Decommission (days later, once v2 is trusted)

Only after v2 has fully earned it: prune the old project's containers/volumes/images. Not before.

---

### Why this is safe by construction
- v2 is an isolated compose project with no host-port publishes → it can't fight the live stack.
- `preflight` is read-only and gates the flip on real parity + image readiness.
- The old stack is only ever `down`ed (never pruned) at the flip, so rollback is one `up`.
- The control-plane's Docker socket access is guard-scoped to `ordo-v2-*` — even the running v2
  cannot touch `ordo-ai-stack-*` containers.

---

## CONSOLIDATION EXECUTED (2026-07-09) — single-location, `main` is production

The stack now runs **entirely from `C:\dev\ordo-ai-stack`** (repo primary checkout), one data root
(`C:\dev\ordo-ai-stack\data`), `main` is the production branch, and the `C:\dev\ordo-v2` worktree is
retired. Compose project name stays `ordo-v2`. Full executed record + evidence: **[`FLIP.md`](./FLIP.md)
→ "CONSOLIDATION EXECUTED".** Summary: PR #72 merged to `main` (two CI defects fixed at the root);
runtime copied to `v2/out/` at the primary checkout with `site.DATA_PATH` repointed to
`C:/dev/ordo-ai-stack/data`; re-rendered on-GPU (compose byte-identical, `.env` diff = DATA_PATH +
the two preserved runtime keys); data **renamed-aside** (never deleted); ~3.75 min chat-path downtime;
validation green incl. the **0-`ordo-v2`-mounts** proof; worktree retired (payload renamed aside one
generation); 29 exited V1 containers removed, **6 volumes + all images KEPT** (V1 reconstitutable).

### Cleanup candidates for a SEPARATE deliberate PR (do NOT delete in a cutover pass)
- **Dead legacy repo-root `ops-controller/`** — the V2 `ops-api` builds from `docker/ops-api` (its
  Dockerfile `COPY main.py` from that context), NOT from the repo-root `ops-controller/`. That
  top-level copy is unused legacy V1 code (the drift flag from `c2cec48`'s report). Remove it in a
  dedicated cleanup PR after confirming no other consumer references it.
- **Operator-retained soak artifacts** (delete after a soak day, not now):
  `C:\dev\ordo-ai-stack\data-v1-snapshot\`, `C:\dev\ordo-v2-data-retired\`,
  `C:\dev\ordo-v2-out-retired\` (contains the old `secrets.env` — shred, don't just delete-to-recycle).
- **V1 prune (after soak, operator runs — NOT reversible):** containers are already removed; to
  reclaim storage: `docker volume rm ordo-ai-stack_caddy_config ordo-ai-stack_caddy_data
  ordo-ai-stack_grafana-data ordo-ai-stack_hermes-data ordo-ai-stack_openclaw-extensions
  ordo-ai-stack_prometheus-data` then `docker image prune -a` **EXCEPT keep
  `ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470`** (shared with the running V2 llamacpp).
