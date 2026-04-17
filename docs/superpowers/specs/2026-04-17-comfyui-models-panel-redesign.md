# ComfyUI models panel — capability-first redesign

**Date:** 2026-04-17
**Status:** Design approved, awaiting implementation plan
**Component:** `dashboard/static/index.html` — "ComfyUI — diffusion models" section

## Motivation

The current panel is too pedantic. It exposes two stacked lists:

1. A raw per-file listing grouped by ComfyUI category (checkpoints, vae, vae_approx, unet, text_encoders, ...). This surfaces noise the user never cares about: 0-KB `put_taesd_*_here` placeholder files that ship with ComfyUI, a dozen TAESD encoder/decoder weights that were pre-bundled, and every intermediate file inside every pack.
2. A checkbox catalog of *every* pack from `models.json` — installed and uninstalled mixed together — with a "Download Selected Packs" button. Half of the list is permanently irrelevant for a given host.

The user's mental model when looking at this panel is "what can this ComfyUI instance do, and how much disk is it eating?" Nothing in the current UI answers that directly.

## Goals

- The panel communicates **capabilities** (video-gen, image-gen, upscale, etc.) at a glance, not file paths.
- Disk usage is visible immediately without scrolling or summing.
- Raw files exist only when the user drills in to clean up — not by default.
- Uninstalled packs disappear from this panel entirely; the "Hub Downloads" section above is the only UI path for pulling new models.

## Non-goals

- No changes to the backend pull pipeline (`scripts/comfyui/pull_comfyui_models.py`, `/api/comfyui/pull`, pull-status endpoints). The CLI + Hub Downloads flow is unchanged.
- No workflow-readiness indicator ("is the flux2-dev workflow ready to run?"). That was considered as Direction C during brainstorming and deferred — it requires workflow-to-pack metadata that does not yet exist.
- No discovery UI for uninstalled curated packs on this page. Deliberately dropped.

## Design

### Layout

```
┌─ ComfyUI models ────────────────────────────────────────┐
│ 65.4 GB on disk · 8 packs installed   [Sync to OpenClaw]│
│                                                         │
│ ████████████████░░░░░░░░████░██░░                       │
│  Video 32 GB   Image 25 GB   Enc 5 GB ▪Upscale ▪Style   │
│                                                         │
│ ▸ Video generation          3 packs  32.0 GB            │
│ ▸ Image generation          2 packs  25.1 GB            │
│ ▸ Text encoders             2 packs   5.4 GB            │
│ ▸ Upscaling                 1 pack    5.4 GB            │
│ ▸ Style LoRAs               1 pack    1.2 GB            │
└─────────────────────────────────────────────────────────┘
```

**Header row.** Total disk + installed pack count on the left; `Sync to OpenClaw` button on the right (moved up from the current bottom-of-panel position).

**Disk bar.** One horizontal stacked bar spanning the panel width. One colored segment per capability group, proportional to bytes on disk. A short legend beneath the bar labels each segment with capability name + human size. Segments smaller than 1% of total collapse into a single combined "other" tick to avoid a sliver of unreadable chrome.

**Capability rows.** One row per capability that has ≥1 installed pack. Row contents: chevron · capability label · pack count · total size. Capabilities with zero installed packs are hidden entirely. Sort: descending by total size.

**Drill-down.** Click a capability row to expand — reveals the list of packs inside that capability, each row showing pack name, status badge (`✓ installed` or `△ X/Y` partial), and pack size. Click a pack row to expand its file list (file name, size, × delete button per file).

**Empty panel.** If no packs are installed, render a single message: "No models installed. Pull models from Hub Downloads above."

### Capability groups

A fixed curated mapping, stored as a new `capability` field on each pack entry in `scripts/comfyui/models.json`:

| Capability key | Label | Packs |
|---|---|---|
| `video` | Video generation | ltx-2.3-fp8, ltx-2.3-gguf, ltx-2.3-extras, ltx-2.3-t2v-basic |
| `image` | Image generation | sd15, sd35-medium, sdxl, flux-schnell, flux1-dev, flux1-dev-gguf, flux2-dev-gguf |
| `encoder` | Text encoders | gemma-3-text-encoder-fp4, gemma-3-text-encoder-gguf, gemma-4-text-encoder, flux2-text-encoder, gemma-3-abliterated-lora |
| `upscale` | Upscaling | supir-upscaler |
| `style` | Style LoRAs | scooby-doo-game-assets |

Text encoders are their own group rather than folded into image/video, because they are shared infrastructure across both and a dedicated group keeps the bar segments readable. The Gemma abliterated LoRA lives in `encoder` because it is an encoder addon, not a visual style.

Unknown/missing `capability` values fall back to a final `other` group so new packs without metadata do not disappear.

### Data flow

1. `/api/comfyui/packs` already returns per-pack `installed_count`, `model_count`, and `description`. It will additionally pass through the new `capability` field read from `models.json`.
2. `/api/comfyui/models` already returns per-file metadata (category, name, size_bytes). The frontend will call both endpoints, then:
   - Build a set of **installed packs** (`installed_count > 0`).
   - Look up each installed pack's files in the `/api/comfyui/models` response by matching filename + destination category (the pack's `models[].file` and `models[].dest`).
   - Sum file sizes per pack → per capability.
   - Render the layout above.
3. Files that are on disk but not claimed by any installed pack (stray downloads, ComfyUI placeholder files) are **not shown**. This is a deliberate behavior change: the raw file browser is gone.

### Interactions

- Chevron expand/collapse state for capabilities is persisted via `localStorage['comfyui-open-caps']` (Set of capability keys serialized as JSON array). Default on first load: all capabilities collapsed.
- Pack-level expand/collapse is session-only; no persistence.
- Per-file × delete uses the existing delete endpoint. After successful delete, the panel reloads.
- `Sync to OpenClaw` button behavior is unchanged.

## Code touch points

- **`scripts/comfyui/models.json`** — add `"capability": "<key>"` to each pack entry.
- **`dashboard/app.py`** — `/api/comfyui/packs` response: surface the new `capability` field from each pack. No new endpoints.
- **`dashboard/static/index.html`** —
  - HTML (lines ~1407–1425): strip the current `<div id="comfyui-models">`, `<div id="comfyui-packs">`, and `<button id="comfyui-pull">` markup. Replace with a single panel root (`<div id="comfyui-panel">`) holding the new header, bar, and capability list.
  - JS (lines ~2234–2321): delete `loadComfyuiModels()` and `loadComfyuiPacks()`. Replace with a single `loadComfyuiPanel()` that fetches both endpoints, computes the capability grouping, and renders. Delete the `comfyuiOpenCategories` state (category-level file browser expansion — no longer needed). Delete the `comfyui-pull` click handler and pull-progress log wiring (the pack-picker download UI is gone; Hub Downloads remains for new pulls).
  - CSS: add rules for `.comfy-disk-bar` (flex row of colored segments) and reuse existing row styles for the capability/pack/file tiers.

## Testing

- `loadComfyuiPanel()` called with: no installed packs, one installed pack, partial pack (files present but `installed_count < model_count`), all capabilities installed. Snapshot markup in each case.
- Manual verification: delete a file via the × button → panel reloads, file gone, pack row reflects new size / may drop out if now empty.
- Manual verification: on a host with placeholder `put_taesd_*_here` files present, confirm they do not appear.
- Existing CLI pull flow (`python scripts/comfyui/pull_comfyui_models.py ...`) still works end-to-end after the `capability` field is added to `models.json`.

## Out of scope (future work)

- Workflow-readiness badges (Direction C from brainstorming) — blocked on workflow-to-pack dependency metadata.
- Inline "Add more model packs" catalog browser — deliberately deferred; Hub Downloads is the single entry point.
