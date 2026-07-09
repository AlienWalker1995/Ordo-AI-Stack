# Ordo v2 substrate — config render engine (first slice)

This directory is the **first slice** of the Ordo v2 rebuild (branch `arch/v2-substrate`).
It is built in isolation — it does **not** touch or reconfigure the running stack. The engine
renders config into `./out/`, never over the live `.env`.

## Why this exists (from the architecture interrogation)

Nearly every failure of the current stack traced to **config drift**: the LLM context size,
the model choice, and Hermes' `context_length` were hand-set in three places and fell out of
sync (256K in Hermes vs 128K in llama.cpp → a compaction deadlock; a stale model registry vs
`.env`; etc.). The agreed cure is a **declarative source → regenerated config** model:

> One human-editable declarative source (`ordo.yaml`). Everything derived (`.env`, Hermes
> context, model-gateway ctx, compose vars) is **regenerated** from it. Edits to *derived*
> outputs don't survive a re-render — so drift is structurally impossible. An explicit
> `overrides:` block in the source is the escape hatch that *does* survive.

This is the **first slice** because everything else (scheduler, plugins, installer) renders
through it, and it's the direct fix for the #1 pain.

## What's here

| File | Role |
|---|---|
| `ordo.example.yaml` | the declarative source — the single source of truth (hardware, tier, model, plugins, overrides) |
| `catalog/models.yaml` | curated model catalog: each entry has resource requirements **and a sha256** (checksums are mandatory — corrupt weights burned us once) |
| `ordo/hardware.py` | hardware detection (GPU/VRAM/RAM/CPU) + mockable profiles for CI |
| `ordo/catalog.py` | load catalog + **best-fit** model selection with a VRAM headroom reserve (encodes the "don't fill the card" lesson) |
| `ordo/config.py` | load/validate the declarative source |
| `ordo/render.py` | `(source + hardware + catalog + plugins) → RenderedConfig`; writes `out/.env`, `out/hermes.context.json`, `out/manifest.json` |
| `ordo/plugins.py` + `plugins/*/plugin.yaml` | **registry-driven** plugins: each manifest declares hardware needs + a config fragment; the renderer enables what fits (media = NVIDIA-only) and resolves `depends_on` |
| `ordo/scheduler.py` | GPU **scheduler decision engine** — FIFO admission + co-run-when-it-fits + LRU idle-evict (replaces the reactive guardian; pure logic, a process broker drives it later) |
| `ordo/cli.py` | `ordo detect | render | doctor` — the seed of the one-script |
| `tests/` | 22 tests: mocked-profile render (5090 + CPU-only), drift-revert, ctx consistency, plugin gating/deps, scheduler co-run/FIFO/evict |

## Slices done on this branch
1. **Config render engine** — declarative source → drift-proof config + hardware right-sizing + checksummed catalog. ✅
2. **Plugin registry** — data-only manifests, hardware-gated, dependency-resolved. ✅
3. **Scheduler decision engine** — FIFO + co-run-if-fits + LRU idle-evict. ✅ (the process broker that drives it against real containers is a later slice — needs the live stack / operator.)
4. **Guided-setup wizard** — `ordo setup` detects → proposes → writes `ordo.yaml` (headless path = CI). ✅
5. **Full-stack parity render + `ordo parity`** — the renderer now reproduces the complete llama.cpp surface (model/ctx/mmproj/MTP args/…), and `ordo parity --ref <.env>` diffs it. ✅
   **Merge-gate (a) demonstrated live:** `ordo parity` vs the real running `.env` → **PARITY OK** (15 keys, 0 mismatches), read-only — proving the engine regenerates today's hand-tuned config from one source with no drift.
6. **Scheduler status API + `ordo doctor` support bundle** — `Scheduler.status()` emits the busy/idle + free-VRAM + running/queued + ETA JSON the dashboard/agents poll; `ordo doctor [--bundle]` exports a secret-redacted diagnostics bundle. ✅ Demonstrated: a 17GB reel + a 4GB chat **co-run** (chat slips beside the render) — the exact eviction-deadlock that broke primus, gone.
7. **MCP as `kind=mcp` plugins** — an MCP server is a manifest (pinned image + env + tools); the renderer composes enabled ones into `out/mcp-registry.yaml` (drift-free) and flags un-pinned images. Runs on CPU. ✅
8. **Compose rendering** — `ordo render` emits an **isolated, runnable** `docker-compose.yml` (own project/network, no host-port clashes, GPU-gated, profile-gated plugins). ✅
9. **Process broker** — turns scheduler decisions into real container start/stop; the Docker backend is **hard-scoped to the `ordo-v2-` prefix so it can never touch the live stack**. ✅
10. **Control-plane service (`ordo serve` = the `ops-controller` image)** — the substrate over HTTP: `GET /status` (live GPU/scheduler + manifest), `GET/POST /model-config` (drift-safe model switch), `POST /jobs[/complete]` (drive the broker). A real `docker/ops-controller.Dockerfile` (built + smoke-tested) makes the compose ref concrete. ✅
    **Validated live in a container:** switching the model over HTTP rewrote `ordo.yaml` **and** regenerated `.env` in one pass (`LLAMACPP_MODEL` + `LLAMACPP_CTX_SIZE` moved together — the drift bug is structurally impossible); unknown model → 404, source untouched. The socket it mounts to drive the broker is guard-scoped to `ordo-v2-*`, so it still can't touch the live stack.

**60 tests green.** `ordo render` writes the complete stack (`.env` + `docker-compose.yml` + `hermes.context.json` + `manifest.json` + `mcp-registry.yaml`); `ordo serve` runs the control plane that regenerates all of it drift-safely at runtime.

## This completes every operator-independent slice
Right-sizing · drift-proof config (parity-proven live) · plugins · MCP · scheduling + broker ·
isolated runnable compose · **control-plane service (built + validated)** · wizard · diagnostics.
All in one worktree, live stack untouched.

## What genuinely needs you now (can't be automated safely)
- The **dashboard SPA** — a UI to design/build (the control plane already serves the status/control data it needs).
- The **remaining service images** — `agent-hermes`, `dashboard`, `comfyui`, `voice` are refs; those build contexts are stack-specific (ops-controller's image is done).
- The **cutover** — bring `ordo-v2` up beside the live stack, validate, do the GPU handoff, keep the old for rollback. This touches the live stack + the 5090, so it's yours to drive (as you said).

## Acceptance gate for THIS slice (from the plan)
1. Renders a full config from one source with **zero hand-edits**.
2. **Drift-revert**: a hand-edited derived value is corrected on the next render.
3. Renders both a **5090 profile and a mocked CPU-only profile** into valid configs.
4. **Consistency**: the one ctx value is identical across `.env`, Hermes, and model-gateway
   (the exact bug that started this).

## Run the tests (no host Python needed)
```
docker run --rm -v "$PWD:/w" -w /w python:3.11-slim \
  sh -c "pip install -q pyyaml pytest && python -m pytest -q"
```

## Explicitly NOT done here (needs the operator / later slices)
Scheduler/broker, plugin registry runtime, the installer wizard, native path, and the actual
cutover. This slice only proves the render engine. The live stack is untouched.
