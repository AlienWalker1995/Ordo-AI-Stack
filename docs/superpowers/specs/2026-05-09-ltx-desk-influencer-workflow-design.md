# LTX-2.3 Desk Influencer Workflow — Design

**Date:** 2026-05-09
**File to ship:** `data/comfyui-storage/ComfyUI/user/default/workflows/ltx-video/LTX-2.3_-_Desk_Influencer.json` (+ `.README.md`)
**Sibling of:** `LTX-2.3_-_Street_Interview.json`

## Goal

Generate YouTube-influencer-at-desk video clips with **character + background consistency across separate generation runs**. One reusable workflow file that can be flipped between vertical (Shorts) and horizontal (long-form) per run, and between two consistency modes:

- **Anchored mode** (default) — first-frame-injection from a master scene still. Tightest possible character + background lock; every clip starts from the exact same frame.
- **Re-angle mode** — pure T2V with the TalkVid ID-LoRA active and a face reference. Lets you generate a different angle/cut of the same character without committing to a new master still.

## Approach (recommended)

Hybrid I2V + ID-LoRA. Re-uses the entire LTX-2.3 distilled FP8 stack from the street-interview workflow (UNet, Gemma 3 12B CLIP, LTX VAE, taeltx2.3 preview, Juno caption nodes). The only structural changes from street-interview:

1. Add a `LoadImage` node ("Master Frame") feeding I2V conditioning.
2. Add a `LoadImage` node ("ID Reference") wired into the TalkVid ID-LoRA's reference input (used in re-angle mode).
3. Drop the Cameraman LoRA strength from `0.5` → `0.15` (desk content is mostly static; keep a touch of life).
4. Replace the street-specific negative prompt additions with desk-specific ones.
5. Replace the default SHOT DATA with a generic tech-reviewer template.

Master still and ID reference are *workflow inputs*, not generated inside this graph. Users either supply real photos or pre-generate them via a separate FLUX workflow. This keeps the graph focused on the video step (matches the street-interview pattern).

## Inputs (parameters the user fills in per run)

| Param | Node type | Default | Notes |
|---|---|---|---|
| Master scene still | `LoadImage` titled "Master Frame" | — | Required for anchored mode. Bypass this node to enter re-angle mode. |
| ID face reference | `LoadImage` titled "ID Reference" | — | Wired into TalkVid ID-LoRA. Only sampled when ID-LoRA strength > 0. |
| SHOT DATA | text primitive | Generic tech-reviewer-at-desk template (see Default SHOT DATA) | Free-text. Edit per gen. |
| WIDTH | int primitive | `576` | `576` for 9:16, `1024` for 16:9. |
| HEIGHT | int primitive | `1024` | `1024` for 9:16, `576` for 16:9. |
| LENGTH (sec) | int primitive | `10` | Drives downstream frame-count calc. |

## Mode switch

| Mode | Master Frame node | ID-LoRA strength | ID Reference | Use when |
|---|---|---|---|---|
| Anchored *(default)* | active | 0.0 (OFF) | unused | 90% of clips. Same character, same desk, different moments/expressions/dialogue. |
| Re-angle | bypassed | 1.0 (ON) | required | Different angle of same character without re-making a master still. |

The README accompanying the JSON spells the toggle out step-by-step (right-click → Bypass; widget on the LoRA row).

## LoRA configuration

| LoRA | File | Default strength | Default state | Reason |
|---|---|---|---|---|
| Cameraman IC-LoRA | `LTX-2.3-Cameraman-IC-LoRA.safetensors` | `0.15` | ON | Subtle breath. Desks shouldn't feel handheld. Crank to 0.3 for vlog energy, 0.0 for locked tripod. |
| TalkVid ID-LoRA | `LTX-2.3-ID-LoRA-TalkVid-3K.safetensors` | `1.0` | OFF | Mode-switch knob. Toggle ON only in re-angle mode. |
| Distilled LoRA | `ltx-2.3-22b-distilled-lora-384.safetensors` | `0.0` | OFF | UNet is already distilled. Leaving this at zero is correct; >0 would double-distill. |

## Negative prompt — desk-specific additions

Inherits the identity-drift and multi-subject rejections from street-interview, then adds:

- "handheld camera, shaky cam, dutch angle, fisheye, vlog selfie distortion"
- "two desks, multiple desks, room layout changing, walls warping"
- "background morphing, lighting changing mid-shot, props moving on desk"

The "missing microphone" line from street-interview is dropped — desk mics aren't required to be on-frame for influencer content.

## Default SHOT DATA (pre-populated)

```
[VISUAL]: man in his early 30s seated at a modern podcast desk, soft ring light from upper-left, a single condenser mic on a low boom in front of him, monitor glow behind, clean tech-podcast aesthetic. He's looking directly at the camera, talking energetically with hand gestures. Vertical 9:16 framing, chest-up, depth of field on the background.

[SPEECH]: "..."

[SOUNDS]: room tone, soft keyboard tap, distant HVAC.
```

User edits the `[SPEECH]` field (and any visual details) per clip. Wardrobe / face / desk specifics stay vague — the master still + ID-LoRA carry that signal.

## What stays the same as street-interview

- UNet / CLIP / VAE / preview-VAE loaders
- Juno caption nodes (social caption text rendered into MP4)
- `EmptyLTXVLatentVideo` defaulted vertical
- Sugiyama auto-organized layout (Shift+O re-flows after edits)
- `comfyui-node-organizer` group bounding boxes

## Out of scope

- A FLUX-T2I sub-graph to generate the master still inside this workflow. Master still is treated as user-provided input, same as street-interview's reference frame would be.
- A from-scratch desk-influencer style LoRA training run. Mentioned at the bottom of the street-interview README as a future option; same applies here. Park until output is 70% of the way there.
- Multi-character / co-host shots. Negative prompt continues to reject "multiple subjects, two people in frame".

## Open decisions confirmed during brainstorm

- **Hybrid I2V + ID-LoRA**, not pure I2V or pure T2V (user picked "best methodology").
- **One workflow file** with WIDTH/HEIGHT primitives, not two files for vertical vs horizontal.
- **Master still as a workflow parameter** (LoadImage), not a built-in two-stage FLUX→LTX graph.
- Defaults left at Cameraman `0.15` ON and 9:16 vertical; user adjusts per run.

## Acceptance criteria

1. Loading the workflow in ComfyUI shows a single graph with the same group structure as street-interview, plus two `LoadImage` nodes ("Master Frame", "ID Reference").
2. With Master Frame populated and a Queue Prompt, the output MP4 begins from that exact still and animates forward.
3. With Master Frame bypassed, ID-LoRA at 1.0, and a face photo in ID Reference, the output MP4 contains a recognizable likeness of the face reference at a desk matching the SHOT DATA.
4. Flipping WIDTH/HEIGHT to `1024`/`576` produces a 16:9 clip without other edits.
5. Captions render burned-in on the output MP4 (no FFmpeg post-process).
6. README accompanying the JSON documents the mode toggle and tuning knobs in the same shape as the street-interview README.
