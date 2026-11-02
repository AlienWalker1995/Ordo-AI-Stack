```
     ___                    ___              _   _ _   _ ___
  _ | |_  _ _ _  ___  ___ / __|___ _ __  / _|_  _| | | |_ _|
 | || | || | ' \/ _ \|___| (__/ _ | '  \|  _| || | |_| || |
  \__/ \_,_|_||_\___/     \___\___| |_|_|_|  \_, |\___/|___|
                                               |__/
```

# Ordo-ComfyUI

Animated captions, transcription, and AI image generation for ComfyUI.

## Nodes

**Caption nodes** (Ordo/ category)
- **Ordo Caption Style** — pick fonts, colors, animation presets
- **Ordo Transcribe Audio** — audio-to-word-level timestamps via faster-whisper
- **Ordo TTS (Edge)** — text-to-speech using Edge TTS (free, no GPU needed)
- **Ordo Render Captions** — burn styled captions onto video frames
- **Ordo Timing Editor** — manually adjust word timings
- **Ordo Caption Studio** — all-in-one transcribe + style + render

**Utility**
- **Ordo Load Text From File** — load .txt / .srt files as strings

**AI Image Generation** (Ordo/AI category)
- **Ordo Proxy Config** — set URL + API key for a LiteLLM proxy
- **Ordo Nano Banana 2** — text-to-image via Gemini Flash through LiteLLM
- **Ordo Nano Banana 2 Edit** — image editing (up to 6 reference images)

** Video Gen
- ** Ordo Veo** - Video generation with Veo 3.1 (fast/quality) - first/last and image ref.

## Install

Drop this folder into `ComfyUI/custom_nodes/`:

```
ComfyUI/
  custom_nodes/
    Ordo-ComfyUI/        <-- this folder
```

Then install dependencies:

```
cd ComfyUI/custom_nodes/Ordo-ComfyUI
pip install -r requirements.txt
```

Or if you use the ComfyUI portable build:

```
.\python_embeded\python.exe -m pip install -r custom_nodes\Ordo-ComfyUI\requirements.txt
```

## AI Image Generation setup

The Nano Banana 2 nodes talk to a **LiteLLM proxy** — they don't call
any API directly because that would be bad. The proxy is run separately. See the
`litellm-proxy/` README for setup instructions.

Quick version:
1. Start the LiteLLM proxy (`litellm --config litellm_config.yaml`)
2. Add a **Ordo Proxy Config** node, point it at `http://localhost:4000` (replace in prod)
3. Connect it to a **Ordo Nano Banana 2** node (or other node that requires the proxy, you'll know...)
4. Type a prompt, queue it

## Dependencies

| Package | Used by |
|---------|---------|
| faster-whisper | Transcription |
| edge-tts | TTS |
| Pillow | Caption rendering |
| soundfile | Audio I/O |
| requests | LiteLLM proxy calls |

# ordo-comfyui-nodes
