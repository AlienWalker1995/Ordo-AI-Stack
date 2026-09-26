#!/bin/bash
# Reclaim ComfyUI's model RAM when it's idle but bloated, so a leaked model can't
# sit inside WSL and starve Jellyfin's (WSL-side) FFmpeg transcodes. Restarts
# comfyui ONLY when it holds >=12 GiB, no GPU lease is held, no resident is evicted,
# and its render queue reads empty. Any check that cannot be read counts as busy
# (fail closed): a multi-step job holds the lease between renders while the queue is
# momentarily empty, and restarting then would kill the job. The restart goes through
# ops-controller, the one sanctioned path for container lifecycle. Silent unless it acts.
set -o pipefail
OPS="${OPS_CONTROLLER_URL:-http://ops-controller:9000}"
[ -n "$COMFYUI_URL" ] || { echo "ComfyUI idle reclaim: COMFYUI_URL is not set; cannot read the queue"; exit 1; }

MEM=$(docker stats --no-stream --format '{{.MemUsage}}' ordo-comfyui-1 2>/dev/null | awk '{print $1}')
val=$(printf '%s' "$MEM" | grep -oE '[0-9.]+' | head -1)
unit=$(printf '%s' "$MEM" | grep -oE '[A-Za-z]+' | head -1)
gib=0
[ "$unit" = "GiB" ] && gib=${val%.*}
[ "${gib:-0}" -ge 12 ] || exit 0

gpu_idle=$(curl -sf -m 10 -H "Authorization: Bearer $OPS_CONTROLLER_TOKEN" "$OPS/status" | python3 -c "
import json, sys
gpu = json.load(sys.stdin)['gpu']
print('yes' if not gpu['running'] and not gpu['queued'] and not gpu['evicted_residents'] else 'no')
" 2>/dev/null)
[ "$gpu_idle" = "yes" ] || exit 0

# ComfyUI's queue, read through its admission gate (GET /queue passes through without a lease;
# ComfyUI itself is only reachable from the gate's network).
queued=$(curl -sf -m 10 "$COMFYUI_URL/queue" | python3 -c "
import json, sys
q = json.load(sys.stdin)
print(len(q['queue_running']) + len(q['queue_pending']))
" 2>/dev/null)
[ "$queued" = "0" ] || exit 0

if curl -sf -m 120 -X POST -H "Authorization: Bearer $OPS_CONTROLLER_TOKEN" \
     -H "Content-Type: application/json" -d '{"confirm": true}' \
     "$OPS/services/comfyui/restart" >/dev/null; then
  echo "Reclaimed ComfyUI RAM (was holding $MEM while idle): protects transcode headroom"
else
  echo "ComfyUI idle reclaim: ops-controller refused or failed the restart (ComfyUI held $MEM)"
fi
