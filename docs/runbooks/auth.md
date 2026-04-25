# SSO Front Door — Operator Runbook

## Setup checklist (one-time)

1. Google Cloud Console → create OAuth 2.0 Web client.
   - Authorized origin: `https://ordo.<tailnet>.ts.net`
   - Authorized redirect: `https://ordo.<tailnet>.ts.net/oauth2/callback`
2. Capture the Client ID + secret into local `.env` as
   `OAUTH2_PROXY_CLIENT_ID` / `OAUTH2_PROXY_CLIENT_SECRET`.
3. Generate cookie secret. **Must be exactly 16, 24, or 32 raw bytes** —
   not base64-encoded. Use:
   ```
   LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 32
   ```
   Save to `.env` as `OAUTH2_PROXY_COOKIE_SECRET`.
4. Issue Tailscale cert:
   ```
   mkdir -p auth/caddy/certs
   tailscale cert \
     --cert-file auth/caddy/certs/tailnet.crt \
     --key-file  auth/caddy/certs/tailnet.key \
     ordo.<tailnet>.ts.net
   ```
5. Set `CADDY_BIND` in `.env` to your tailnet IP from `tailscale ip -4`.
   The `:?` failsafe in the compose `caddy.ports` mapping refuses to
   start with an empty value — by design, since an empty bind would
   silently degrade to `0.0.0.0:443`.
6. Replace `auth/oauth2-proxy/emails.txt` locally with your real
   allowlist (do **not** commit your real email — repo file stays
   `YOUR_ALLOWLIST_EMAIL`). Run
   `git update-index --skip-worktree auth/oauth2-proxy/emails.txt`
   to suppress accidental staging of the local edit.
7. `docker compose up -d caddy oauth2-proxy`.

## Edit allowlist

Edit your local `auth/oauth2-proxy/emails.txt` (one email per line),
then `docker compose restart oauth2-proxy`. Sessions for removed emails
remain valid until cookie expiry (24h max); to force-invalidate, also
rotate `OAUTH2_PROXY_COOKIE_SECRET` and restart.

## Recovery — Google OIDC outage

When Google sign-in is unreachable, all browser paths fail. Two recovery
levers:

1. **Bearer fallback.** Dashboard's `_verify_auth()` still accepts the
   bearer-token mode for non-proxy requests. From the host:
   ```
   curl -H "Authorization: Bearer $DASHBOARD_AUTH_TOKEN" \
     http://localhost:8080/api/...
   ```
   This requires the dashboard's host-port publish to be re-enabled
   temporarily, which means editing `docker-compose.yml`. Treat as
   emergency-only.

2. **Direct container access.** With `docker exec` you can run any
   verb inside a service container while public access is broken.

## Recovery — oauth2-proxy crash

`docker compose restart oauth2-proxy`. Caddy's `forward_auth` retries
automatically. If oauth2-proxy is unhealthy on boot, check
`docker logs ordo-ai-stack-oauth2-proxy-1` for `OAUTH2_PROXY_*` env
mismatch — the most common cause is a `OAUTH2_PROXY_COOKIE_SECRET` that
isn't exactly 16/24/32 bytes.

## Cookie / session rotation

Rotate cookie secret to force everyone (just you) to re-auth:

```
NEW_SECRET=$(LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 32)
# Update OAUTH2_PROXY_COOKIE_SECRET in .env to $NEW_SECRET
docker compose restart oauth2-proxy
```

## Tailscale cert renewal

Tailscale-issued certs expire roughly every 90 days. Renew:
```
tailscale cert \
  --cert-file auth/caddy/certs/tailnet.crt \
  --key-file  auth/caddy/certs/tailnet.key \
  ordo.<tailnet>.ts.net
docker compose restart caddy
```

Consider scheduling a monthly cron: `0 4 1 * *` runs the above plus
the `restart caddy` to stay well ahead of expiry.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser stuck redirecting | Cookie domain mismatch | Confirm `CADDY_TAILNET_DOMAIN` matches `<tailnet>.ts.net` exactly |
| `redirect_uri_mismatch` from Google | OAuth client redirect URI doesn't match | Update GCP console authorized redirect URI to match `CADDY_TAILNET_HOSTNAME` |
| 502 from Caddy on `/dash/` | Dashboard not on proxy-net | Add `proxy-net` to dashboard's networks; `docker compose up -d --force-recreate dashboard caddy` |
| 401 with "missing X-Forwarded-Email from trusted proxy" | Caddy-to-dashboard subnet differs from `DASHBOARD_TRUSTED_PROXY_NET` | `docker network inspect ordo-ai-stack_proxy-net`. Pin matching CIDR in env |
| oauth2-proxy `cookie_secret must be 16, 24, or 32 bytes` | Used `openssl rand -base64 32` (44 chars) | Use `tr -dc 'a-zA-Z0-9' </dev/urandom \| head -c 32` to get exactly 32 raw bytes |
| `/n8n/webhook/...` returns 302 to oauth2-proxy | Bypass matcher missing | Confirm `auth/caddy/Caddyfile` `@auth not path` line excludes `/n8n/rest/oauth2-credential/callback /n8n/webhook/*` |
| Caddy unhealthy, logs ok | In-container healthcheck targets `http://localhost/healthz` (port 80) | Caddyfile must include the `:80` site block that responds to `/healthz` (separate from the HTTPS host block) |
