# AUDIT — Phase 5.5 systematic runtime-config parity (V2 rendered ↔ V1 live)

**Goal:** kill the whole defect class that rolled back flip attempts #1 and #2 — the render engine
silently omitting a piece of V1's per-service config that only mattered once the container ran.
Every V2 service (rendered from the REAL operator source `v2/ordo.yaml` → `v2/out/`) was diffed
against its live V1 container via `docker inspect` (23 running containers, read-only) across every
runtime dimension. Every gap is fixed **in the render engine / manifests (data-driven)** with a
regression test per defect **class**, not per literal value.

Ground truth: V1 running (24 containers), `docker inspect` 2026-07-09. Real GPU uuids confirmed via
`nvidia-smi` — **5090 (primary/compute) = `GPU-97fe65ee-…`**, **1070 (secondary/voice) =
`GPU-20fac13a-…`** — matching V1's `overrides/gpu-assignments.yml` exactly.

## Dimensions compared (per service)
Entrypoint · Cmd · User · WorkingDir · Mounts (classified: brain→staged / immutable-shared→V1 ro /
config→out copies / secret-files→same host path / docker.sock / named vol) · Env KEYS · ExtraHosts ·
CapAdd/Devices · NVIDIA caps · Healthcheck · depends_on conditions · restart · ports · GPU
reservation + CUDA_VISIBLE_DEVICES pin.

## Tally
- **Services audited:** 23 (all running V1 containers with a V2 counterpart; per PARITY.md the
  4 category-D one-shots are obsolete-by-design, not services).
- **Dimension checks:** ~230 (23 services × ~10 applicable dimensions).
- **MATCH:** ~205 · **INTENTIONAL-DIFF (documented):** ~18 · **GAP-FIXED:** 7 (across 4 classes).

## GAPS FOUND & FIXED (the whole point)

| # | Service(s) | Dimension | What was missing in V2 (live-only failure) | Fix (data-driven) |
|---|---|---|---|---|
| **G1** | `agent` (hermes) | Mounts / User / Env / Healthcheck / depends conditions | **Defect #3.** V2 agent had NO volumes, no user, no env, no file-secrets, no healthcheck, plain-list depends. V1's hermes-gateway has: brain `data/hermes`→`/home/hermes/.hermes`, `/workspace` data tree, `/c/dev` mirror, 2 file-secrets, full env, `gateway_state.json` healthcheck, `service_healthy` gates. | Agent manifest schema extended with `user`/`volumes`/`environment`/`secret_files`/`depends_on`/`healthcheck`; threaded render→compose. Brain→**staged** `${DATA_PATH}/hermes` (never live path). |
| **G2** | `llamacpp` (core) | GPU pin | **Defect #4 (the exact warned class).** V2 rendered `count: all` + NO `CUDA_VISIBLE_DEVICES`. On this dual-GPU WSL2 box that is a **no-op** (per the WSL2-isolation memory) → llama.cpp can see/land the 1070. V1 pins the 5090 uuid via `CUDA_VISIBLE_DEVICES` **and** `device_ids`. | compose.py pins core llamacpp to the resolved **primary** GPU uuid (both layers). |
| **G3** | `comfyui` | GPU pin | Same as G2 — `gpu: true`→`count: all`, no uuid pin. V1 pins comfyui to the 5090. | New `gpu_pin: primary` manifest field → both-layer uuid pin. |
| **G4** | `llamacpp-embed` (rag) | GPU pin | Same as G2. V1 pins llamacpp-embed to the 5090. | `gpu_pin: primary` (CPU-fallback when no GPU). |
| **G5** | `agent` + (renderer) | depends_on **conditions** | V2 emitted plain-list depends everywhere; V1 gates the agent on `service_healthy` (else it 5xx-storms while the gateways warm). | compose renderer now supports `{peer: condition}` long form; agent manifest declares the gates. |
| **G6** | `mcp-gateway` | Mounts (docker.sock + config dir) / Env / Healthcheck | V2 had NO docker.sock (the gateway **spawns MCP servers as containers** — without it, no tools), NO config-dir mount (the wrapper reads `servers.txt`+`registry-custom.yaml` at runtime — without it, empty catalog), missing env keys + healthcheck. | `_mcp_gateway()` adds docker.sock + `./mcp:/mcp-config` + env + healthcheck; `ordo render` now emits the **wrapper-native** `out/mcp/servers.txt` + `out/mcp/registry-custom.yaml` (V1's exact schema) so the same wrapper works unmodified. |
| **G7** | `mcp-gateway` | Rendered artifact schema | The prior `mcp-registry.yaml` was V2's own schema — NOT what the gateway wrapper's `jq`/`--additional-catalog` reads. Mounting it would drift declared-vs-consumed. | Emit the wrapper's native `registry:` catalog + `servers.txt`; keep `mcp-registry.yaml` as a human summary only. |

## INTENTIONAL-DIFFs (documented, NOT gaps)

| Service | Difference | Why it's intentional |
|---|---|---|
| `ops-controller` | V2-native image + `ordo serve` cmd; V1's `comfyui-models`/`runtime.env`/`hf_token` mounts absent | V2-native control plane. Its reactive-guardian mounts belong to the V1 guardian that V2's **scheduler** replaces (PARITY.md). It has what `ordo serve` needs: docker.sock (guard-scoped to `ordo-v2-*`) + `./:/config`. `HF_TOKEN` comes via `secrets.env`. |
| `dashboard` | V2-native SPA; V1's utility-GPU + comfyui/gguf/mcp binds absent | V2-native control plane; model/GPU state is the registry via ops-controller, not host binds. |
| all core UI services | NO host `ports:` (V1 published `127.0.0.1` loopbacks for model-gateway/mcp-gateway/qdrant) | V2 deployment model: reached via the edge (Caddy) only; loopback publishes were host-debug. **Only caddy `:443`** publishes (confirmed in resolved config). |
| `model-gateway`/`mcp-gateway` | project buildable images `ordo-v2/*` vs V1 `ordo-ai-stack-*:latest` | Same wrapper build, project-namespaced + pinned by build context (preflight “build first”, no float). |
| `llamacpp-embed`/`qdrant`/`rag-ingestion` | live only behind `--profile rag` | Grouped into the `rag` plugin (PARITY.md) — only needed when RAG is on. |
| `gpu-exporter` | keeps `count: all` (no uuid pin) | Matches V1 — the exporter monitors ALL cards by design. |
| brain / mutable data binds | point at `${DATA_PATH}` (V2 staged) not V1 `data/*` | The whole beside-run premise: V2 runs on staged copies; immutable big content (GGUF/comfyui models) is shared read-only from `${BASE_PATH}` (V1 tree). |
| `searxng`/`stt` restart etc. | one V1 container (`tts`) shows an older compose-label path form | V1-side staleness (a not-recreated container), not a V2 gap; V2 renders the current form. |

## Verification (offline, no stack touched)
- **Tests:** ruff clean + **134 passed** (was 122; **+12** across the 4 new defect classes:
  primary-GPU-pin, voice-secondary-pin, gpu-pin-fallback, depends-conditions, mcp-gateway-wiring,
  wrapper-native-mcp-config, and 5 agent-runtime-wiring guards).
- **`docker compose config`** — real, ALL 10 profiles (`rag webui automation search codebase-memory
  hermes-ui monitoring media voice edge`) → **exit 0**, zero warnings. Resolved config confirms:
  only caddy publishes `:443`; llamacpp/comfyui/llamacpp-embed pinned to the 5090 uuid; stt/tts to
  the 1070 uuid; agent brain → the staged path; both file-secrets read-only.
- **`ordo preflight --ref <live .env> --secrets out/secrets.env`** → **GO**: ctx consistent (131072),
  model sha256-pinned, MCP images digest-pinned, GPU present, **parity vs live .env: 24 keys, 0
  mismatch**, project images built, all required secrets set.

## Confidence for attempt #3
The defect class that rolled back #1/#2 (render silently drops V1 per-service config) is now covered
by data-driven schema + a regression test per class. What remains verifiable only **live** (honest):
container **entrypoint-script internal behavior** (the hermes `/entrypoint.sh` chmod/gosu drop; the
mcp `gateway-wrapper.sh` actually spawning a non-empty catalog against the real docker.sock; the
llamacpp wrapper argv) and **first-boot data migrations** on the staged brain. Config **shape** is
now at parity; runtime behavior of those scripts is the only class left, and each has a fast rollback
(`stop` V2 / `start` V1).
