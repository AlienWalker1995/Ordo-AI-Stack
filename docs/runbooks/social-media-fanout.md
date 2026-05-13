# Social Media Fan-out Workflow

Single n8n webhook → routes the same post payload to Instagram, Facebook, and TikTok branches. Replaces the previous one-cron-per-platform pattern.

## Architecture

```
Hermes cron (or any caller)
    │ POST /webhook/social-fanout
    ▼
n8n Webhook node
    │
    ▼
Split platforms[] → Switch by platform
    ├─→ instagram branch (HTTP: /media → /media_publish)
    ├─→ facebook branch  (placeholder — TODO: wire Page API)
    ├─→ tiktok branch    (placeholder — TODO: wire Content Posting API)
    └─→ unknown          → "rejected" result
    │
    ▼
Merge (append mode) → Aggregate → Respond
```

Caller gets back one JSON object with a `results` array, one entry per platform requested. Partial failures are reported per-branch; no single branch failure breaks the response.

## Files

| Path | Purpose |
|---|---|
| `n8n/workflows/social_media_fanout.json` | Canonical workflow definition. Source of truth — n8n's database state is rebuilt from this. |
| `scripts/n8n/deploy_workflow.py` | Idempotent deploy: seeds n8n Variables, creates/updates the workflow, activates it. |

## Deploy

The deploy script needs:

- The n8n API key (decrypted to `~/.ai-toolkit/runtime/secrets/n8n_api_key` by `scripts/secrets/decrypt.sh`).
- Network reach to n8n. From the host the port isn't published; run inside the docker network:

```sh
docker run --rm --network ordo-ai-stack-backend \
  -v "$PWD:/repo:ro" \
  -v "$HOME/.ai-toolkit/runtime/secrets:/secrets:ro" \
  -e N8N_API_URL=http://n8n:5678 \
  -e N8N_API_KEY_FILE=/secrets/n8n_api_key \
  python:3.12-slim sh -c "cd /repo && python scripts/n8n/deploy_workflow.py"
```

On success it prints:
- The webhook URL (`http://n8n:5678/webhook/social-fanout` over the docker network; reach it externally via Caddy at `https://<tailnet-host>/n8n/webhook/social-fanout`).
- The workflow ID.

Re-run any time the JSON changes; the script updates the existing workflow in place.

## Credentials

The Instagram branch reads `$vars.IG_USER_ID` and `$vars.IG_ACCESS_TOKEN` as n8n **Variables** (separate from n8n Credentials — Variables are encrypted at rest by n8n's instance `encryptionKey` and accessible via workflow expressions).

The deploy script tries to populate these from `~/.instagram_creds` on the host (expects `IG_USER_ID=...` and `ACCESS_TOKEN=...` lines). When that file isn't on the host — e.g. it lives inside the Hermes container at `/home/hermes/.instagram_creds` — set them by hand:

```sh
KEY=$(cat ~/.ai-toolkit/runtime/secrets/n8n_api_key)
docker exec ordo-ai-stack-mcp-gateway-1 \
  curl -s -X POST -H "X-N8N-API-KEY: $KEY" -H "Content-Type: application/json" \
  -d '{"key":"IG_USER_ID","value":"<id>"}' http://n8n:5678/api/v1/variables
docker exec ordo-ai-stack-mcp-gateway-1 \
  curl -s -X POST -H "X-N8N-API-KEY: $KEY" -H "Content-Type: application/json" \
  -d '{"key":"IG_ACCESS_TOKEN","value":"<token>"}' http://n8n:5678/api/v1/variables
```

FB and TT branches are placeholders. They report `status: "skipped"` until you:
1. Acquire credentials (FB Page access token w/ `pages_manage_posts`; TikTok Content Posting API access — gated for business accounts).
2. Replace the placeholder Set nodes in the workflow with real HTTP Request nodes against the respective Graph APIs.
3. Add `FB_PAGE_ID` / `FB_PAGE_ACCESS_TOKEN` / `TIKTOK_*` n8n Variables.
4. Re-deploy.

## Calling it

Request body:

```json
{
  "caption": "Today's AI digest …",
  "image_url": "https://your-cdn.example/image.jpg",
  "platforms": ["instagram", "facebook", "tiktok"]
}
```

Response (current state — IG creds not yet set, FB/TT not wired):

```json
{
  "status": "ok",
  "results": [
    {"platform": "instagram", "status": "error", "reason": "connect ECONNREFUSED 0.0.0.0:443"},
    {"platform": "facebook",  "status": "skipped", "reason": "TODO: wire FB Page access token + /me/feed call"},
    {"platform": "tiktok",    "status": "skipped", "reason": "TODO: wire TikTok Content Posting API …"}
  ]
}
```

Once IG variables are set, the instagram branch transitions to `status: "ok"` with `post_id` + `post_url` fields.

## Rewiring the Hermes cron

The existing `Instagram AI News Post` Hermes cron (id `e19135837aaf`) calls `instagram_post.sh` directly. To rewire onto the fan-out:

1. Generate the post payload in the cron prompt (caption + image URL) as you do today.
2. Replace the final IG-posting step with:

   ```sh
   curl -s -X POST -H "Content-Type: application/json" \
     -d "$(jq -n --arg cap "$CAPTION" --arg url "$IMAGE_URL" \
          '{caption:$cap,image_url:$url,platforms:["instagram","facebook","tiktok"]}')" \
     http://n8n:5678/webhook/social-fanout
   ```

3. Discord delivery of the cron still happens via Hermes's existing `deliver: discord:…` config — the webhook response goes into the cron's `last_run_*` fields and is shown in the dashboard.

The existing cron and `instagram_post.sh` remain in place as a fallback until the fan-out path has been observed working for a few cycles.

## Roll back

```sh
KEY=$(cat ~/.ai-toolkit/runtime/secrets/n8n_api_key)
WF_ID=$(docker exec ordo-ai-stack-mcp-gateway-1 sh -c \
  "curl -s -H 'X-N8N-API-KEY: $KEY' http://n8n:5678/api/v1/workflows?name=Social%20Media%20Fan-out" \
  | jq -r '.data[0].id')
docker exec ordo-ai-stack-mcp-gateway-1 \
  curl -s -X POST -H "X-N8N-API-KEY: $KEY" http://n8n:5678/api/v1/workflows/"$WF_ID"/deactivate
```

Or delete entirely: `DELETE /api/v1/workflows/<id>`.
