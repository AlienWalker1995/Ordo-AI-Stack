#!/bin/sh
# Start the TTS server, then warm it up once. A fresh container's first syntheses are slow while
# CUDA loads kernels and tunes them (18-24 s per reply measured on the 1070, 2026-10-15); one throwaway
# request at start moves that cost off the first real reply. The server stays the main process: this
# shell forwards stop signals to it and exits with its status.
set -eu

python3 server.py &
server=$!
trap 'kill -TERM "$server" 2>/dev/null' TERM INT

python3 - <<'PY' || echo "start.sh: warm-up skipped (server not ready); first reply will be slow" >&2
import json, time, urllib.request
base = "http://127.0.0.1:8004"
deadline = time.time() + 600
while time.time() < deadline:
    try:
        if json.load(urllib.request.urlopen(f"{base}/api/model-info", timeout=5)).get("loaded"):
            break
    except Exception:
        pass
    time.sleep(3)
else:
    raise SystemExit(1)
body = json.dumps({"model": "chatterbox", "input": "Warming up the voice.", "voice": "hermes.wav",
                   "response_format": "wav"}).encode()
for _ in range(2):  # the second pass catches kernels the first one compiled lazily
    urllib.request.urlopen(urllib.request.Request(f"{base}/v1/audio/speech", data=body,
                           headers={"Content-Type": "application/json"}), timeout=300).read()
print("start.sh: TTS warmed up", flush=True)
PY

wait "$server"
