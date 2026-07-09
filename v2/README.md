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

## Next (approaching the operator boundary)
`ordo doctor` support-bundle · a status-API contract (busy/ETA the dashboard polls) · the process
**broker** that drives the scheduler against real containers · the dashboard SPA · the **cutover**.
The last few touch the live stack or need decisions — reserved for the operator.

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
