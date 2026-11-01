"""ComfyUI HTTP endpoint for the Rick/Morty dialogue reel render.

Thin bridge so Hermes (which has no ffmpeg/whisper/rembg and can't docker-exec)
can trigger the multi-step render_reel_dialogue.py over the ComfyUI API â€” the same
way it triggers the brainrot reel workflow. The render itself is unchanged.

Why a subprocess + async job (not a blocking call, not a custom node):
  * The render POSTs to THIS server's /prompt for each VibeVoice line. Running it
    in-process (a node) or blocking the event loop would deadlock that. Launching
    it as an ASYNC subprocess keeps the event loop free to service those /prompt
    calls â€” no deadlock.
  * The render holds GPU residency for its whole duration, and the ops-controller
    scheduler evicts llamacpp from the card to grant it. (The old
    COMFYUI_SERIALIZE_LLAMACPP guardian that used to do this is retired — there is
    exactly one GPU arbiter now.) Hermes therefore runs degraded on llamacpp-cpu
    mid-render rather than going dark, but an async job (POST returns a job_id
    immediately, GET polls) still fits best: Hermes fires, ends its turn, and reads
    the result on a later tick once the scheduler has restored the GPU model.

Routes:
  POST /dialogue_reel/render   body {"dialogue": {...}, "tts": "vibevoice"}
                               -> {"ok": true, "job_id": "...", "status": "running"}
  GET  /dialogue_reel/status?job_id=...
                               -> {"ok": true, "status": "running|done|error", "result": {...}}
"""
import asyncio
import json
import os
import time

from aiohttp import web
from server import PromptServer

SCRIPT = "/root/ComfyUI/scripts/render_reel_dialogue.py"
INPUT_DIR = "/root/ComfyUI/input/rickmorty"
_JOBS: dict = {}  # job_id -> {"status", "result", "started"}


# The render's only GPU work is the prompts it queues on this server's /prompt, which ComfyUI runs
# in its own process under the GPU admission gate's lease. Everything the subprocess does itself
# (whisper checks and captions, voice similarity, rembg, ffmpeg) must stay off the card: the gate
# releases the lease once the queue drains, and the scheduler then restores llama.cpp. Hiding the
# devices makes that structural rather than a per-call device= choice.
_RENDER_ENV = {**os.environ, "CUDA_VISIBLE_DEVICES": ""}


async def _run_render(job_id: str, dialogue_path: str, tts: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "python3", SCRIPT, "--dialogue-json", dialogue_path, "--tts", tts,
        cwd="/root/ComfyUI", env=_RENDER_ENV,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    tail = [ln for ln in out.decode("utf-8", "ignore").splitlines() if ln.strip()]
    try:
        result = (json.loads(tail[-1]) if tail else
                  {"ok": False, "error": "no output",
                   "exit_code": proc.returncode,
                   "stderr_tail": err.decode("utf-8", "ignore")[-600:]})
    except Exception:
        result = {"ok": False, "error": "render output unparseable",
                  "stdout_tail": out.decode("utf-8", "ignore")[-600:],
                  "stderr_tail": err.decode("utf-8", "ignore")[-600:]}
    _JOBS[job_id] = {"status": "done" if result.get("ok") else "error",
                     "result": result, "started": _JOBS.get(job_id, {}).get("started")}


@PromptServer.instance.routes.post("/dialogue_reel/render")
async def _start(request):
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response({"ok": False, "error": f"bad json: {e}"}, status=400)
    dialogue = body.get("dialogue")
    tts = body.get("tts", "vibevoice")
    if not isinstance(dialogue, dict) or not dialogue.get("lines"):
        return web.json_response(
            {"ok": False, "error": "body.dialogue must be an object with a non-empty lines[]"},
            status=400)
    slug = dialogue.get("slug") or f"rm_{int(time.time())}"
    os.makedirs(INPUT_DIR, exist_ok=True)
    dpath = os.path.join(INPUT_DIR, f"dialogue_{slug}.json")
    with open(dpath, "w", encoding="utf-8") as f:
        json.dump(dialogue, f)
    job_id = f"{slug}_{int(time.time())}"
    _JOBS[job_id] = {"status": "running", "result": None, "started": int(time.time())}
    asyncio.ensure_future(_run_render(job_id, dpath, tts))
    return web.json_response({"ok": True, "job_id": job_id, "status": "running", "slug": slug})


@PromptServer.instance.routes.get("/dialogue_reel/status")
async def _status(request):
    job_id = request.query.get("job_id", "")
    j = _JOBS.get(job_id)
    if not j:
        return web.json_response({"ok": False, "error": f"unknown job_id {job_id!r}"}, status=404)
    return web.json_response({"ok": True, "job_id": job_id, **j})


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
print("[dialogue_reel_endpoint] routes registered: "
      "POST /dialogue_reel/render, GET /dialogue_reel/status")
