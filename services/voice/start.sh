#!/bin/sh
# Start the TTS server, then warm it up. A fresh container's first syntheses are slow while CUDA
# loads and tunes kernels (18-24 s per reply measured on the 1070, 2026-10-15); throwaway requests at
# start move that cost off the first real reply.
#
# The server stays the main process. The warm-up runs beside it in the background, so a stop signal
# (docker stop, or the GPU arbiter yielding the card) reaches the server at once, even mid warm-up.
# The script exits only after the server has exited, with the server's status.
set -u

python3 server.py &
server=$!

SERVER_PID=$server python3 - <<'PY' &
import json, os, time, urllib.request

base = "http://127.0.0.1:8004"
server_pid = int(os.environ["SERVER_PID"])


def server_alive():
    try:
        os.kill(server_pid, 0)
        return True
    except ProcessLookupError:
        return False


deadline = time.time() + 600
while True:
    if not server_alive():
        raise SystemExit(0)  # the server died; its own exit status is what matters
    try:
        if json.load(urllib.request.urlopen(f"{base}/api/model-info", timeout=5)).get("loaded"):
            break
    except Exception:
        pass
    if time.time() > deadline:
        print("start.sh: warm-up skipped (model not loaded in 600 s); first reply will be slow", flush=True)
        raise SystemExit(0)
    time.sleep(3)

body = json.dumps({"model": "chatterbox", "input": "Warming up the voice.", "voice": "Emily.wav",
                   "response_format": "wav"}).encode()
for _ in range(2):  # the second pass catches kernels the first one compiled lazily
    urllib.request.urlopen(urllib.request.Request(f"{base}/v1/audio/speech", data=body,
                           headers={"Content-Type": "application/json"}), timeout=300).read()
print("start.sh: TTS warmed up", flush=True)
PY
warm=$!

trap 'kill -TERM "$server" "$warm" 2>/dev/null' TERM INT

# `wait` returns early when a trapped signal arrives, while the server may still be shutting down,
# so keep waiting until the server process itself is gone; its status is the last `wait` result.
while :; do
    wait "$server"
    status=$?
    kill -0 "$server" 2>/dev/null || break
done
kill "$warm" 2>/dev/null
exit "$status"
