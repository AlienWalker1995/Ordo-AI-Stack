## Running inside an Ordo stack: read this before "Quick Start"

ComfyUI already runs as the stack's `comfyui` service. The parts of this skill written for a
single machine do not apply here.

- **Never install or launch ComfyUI from this container.** Skip `comfyui_setup.sh`,
  `comfy install`, `comfy launch` and `hardware_check.py`. The stack owns the server, its models
  and its custom nodes; for a node pack's Python requirements use the
  `comfyui__install_custom_node_requirements` tool.
- **Submit through `$COMFYUI_URL`, never to `comfyui:8188`.** The stack points `COMFYUI_URL` at
  its GPU admission gate and the scripts here default to it, so run them without `--host`. The
  gate takes the scheduler's GPU lease before a render starts, evicting the resident language
  model, and releases it once the queue drains. Submitting to the engine directly skips the lease,
  so the render and the resident model share the GPU, and near full VRAM that can take down the
  whole host. Wherever the docs below show `http://127.0.0.1:8188`, read `$COMFYUI_URL`. Only
  `/prompt` is gated: status, history, upload and view calls pass straight through unchanged.
- **A render evicts the model you are running on.** While it runs, your turns fall back to the
  CPU model if the stack has one (much slower), or fail until the GPU is free. Do not queue a job
  and then poll it turn by turn. `run_workflow.py` submits, waits and downloads in one process;
  run it, then report. Video and 3D jobs can take many minutes.
- **Inputs go over HTTP.** ComfyUI's `input/` directory is in a volume this container cannot see.
  Pass `--input-image` to `run_workflow.py`, or `POST $COMFYUI_URL/upload/image` (multipart field
  `image`, optional `subfolder`), then reference the returned filename in `LoadImage` or
  `LoadAudio`.
- **Outputs** land in ComfyUI's output directory, which this container sees at
  `/workspace/data/comfyui-output/`. `run_workflow.py` also downloads them to `--output-dir`.
- **"Not reachable" right after a restart means it is still booting.** The server spends one to
  five minutes loading custom-node dependencies before it listens, and restarting again starts
  that over. Poll `$COMFYUI_URL/system_stats` every ten seconds or so for up to five minutes, and
  restart only when the logs show a real traceback. A restart drops queued and running jobs, so
  resubmit afterwards.
- **The `comfyui__*` MCP tools** cover the same ground from chat: `queue_prompt`, `run_workflow`,
  `list_workflows`, `get_comfyui_queue`, `get_comfyui_history`, `interrupt_comfyui`, and model
  listing and downloads. They submit through the same gate.

