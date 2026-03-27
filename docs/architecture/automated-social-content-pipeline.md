# Automated social content pipeline (target architecture)

**Goal:** OpenClaw (or scheduled automation) **generates video** (ComfyUI / local workflows), then **publishes** to social platforms with **no routine human step** — subject to your policies, API limits, and platform ToS.

This repo **does not** ship turnkey “post everywhere” code. It **does** provide the pieces to assemble that pipeline.

## Pipeline stages

| Stage | What happens | Typical stack |
|-------|----------------|---------------|
| **1. Decide** | Topic, prompt, schedule, or event trigger | OpenClaw **cron** / channels; optional LLM for ideation |
| **2. Generate** | Render video (frames, encode) | **ComfyUI** via MCP **`comfyui__run_workflow`**; API-format workflows under `data/comfyui-workflows/` |
| **3. Normalize** | Trim, codec, thumbnail, captions, watermark | ffmpeg, extra nodes, or n8n **Code** / **Execute Command** |
| **4. Publish** | Upload + metadata per platform | **Platform APIs** (OAuth, upload endpoints) — usually **n8n** for retries, OAuth refresh, branching |
| **5. Observe** | Logs, failures, rate limits | n8n + OpenClaw notifications; Discord/Telegram |

## How Ordo AI Stack services fit

| Service | Role |
|---------|------|
| **OpenClaw** | Policy, memory, scheduling, Discord/Telegram; can invoke MCP **gateway__call** |
| **MCP gateway** | ComfyUI + n8n + search + … **one** URL for tools |
| **ComfyUI** | GPU execution; outputs on disk / MCP response |
| **n8n** | **Publish** workflows, credentials, retries, “if file exists then upload” |
| **Dashboard** | Models, Comfy restarts, `pip` for nodes |

## Recommended integration pattern

1. **Stabilize generation** — fixed LTX/SD workflow ids, known resolution, automated model pulls.
2. **Stable output path** — e.g. `data/comfyui-output/` or a named file pattern n8n can **Read Binary** / watch.
3. **n8n owns posting** — one sub-workflow per platform or one **Switch** with credentials; handle **429**, token refresh, and size limits.
4. **OpenClaw owns “what and when”** — calls Comfy via MCP; triggers n8n via **Webhook** or **HTTP Request** node when a render completes (or n8n polls on a schedule).
5. **Guardrails** — daily caps, duplicate detection, kill-switch env var; optional **manual approval** step in n8n before `POST` if you need a safety valve.

## What you must implement outside this repo

- **Developer apps** / OAuth for each network (Meta, X, YouTube, TikTok Business, LinkedIn, etc.).
- **Upload** semantics (codec, duration, aspect ratio, captions).
- **Compliance** — copyright, disclosure, geo restrictions, account health; **fully unattended** posting carries **ban and legal** risk.

## Optional: embedded Comfy pack

[ComfyUI-OpenClaw](https://github.com/rookiestar28/ComfyUI-OpenClaw) is a **different** product (orchestration **inside** Comfy + chat connectors). It does **not** replace n8n posting or platform APIs; evaluate separately if you want Comfy-centric automation.

## Related

- [mcp/docs/comfyui-openclaw.md](../../mcp/docs/comfyui-openclaw.md) — MCP + ComfyUI + n8n parity
- `mcp/README.md`, `docs/runbooks/TROUBLESHOOTING.md`
