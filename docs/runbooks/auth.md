# SSO Front Door — Operator Runbook

## Setup checklist (one-time)

1. Google Cloud Console → create an OAuth 2.0 Web client.
   - Authorized origin: `https://ordo.<tailnet>.ts.net`
   - Authorized redirect: `https://ordo.<tailnet>.ts.net/oauth2/callback`
   - This single `:443` redirect URI covers the whole stack: the
     port-per-service model (`:443` front door plus `:8443`–`:8448`)
     shares one domain-scoped oauth2-proxy cookie — no per-port URIs.
2. Capture the Client ID + secret into `.env` as
   `OAUTH2_PROXY_CLIENT_ID` / `OAUTH2_PROXY_CLIENT_SECRET`.
3. Generate a cookie secret — **exactly 16, 24, or 32 raw bytes**, not
   base64:
   ```
   LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 32
   ```
   Save to `.env` as `OAUTH2_PROXY_COOKIE_SECRET`.
4. Issue the Tailscale cert:
   ```
   mkdir -p auth/caddy/certs
   tailscale cert \
     --cert-file auth/caddy/certs/tailnet.crt \
     --key-file  auth/caddy/certs/tailnet.key \
     ordo.<tailnet>.ts.net
   ```
5. Set `CADDY_BIND` in `.env`. Use your tailnet IP (`tailscale ip -4`)
   to restrict Caddy to the tailnet interface, or `0.0.0.0` to bind all
   interfaces (LAN included; the router's NAT remains the boundary). The
   `:?` failsafe on every `caddy.ports` mapping refuses only an
   empty/unset value.
6. Replace `auth/oauth2-proxy/emails.txt` with your real allowlist (one
   email per line). Do **not** commit it — the repo file stays
   `YOUR_ALLOWLIST_EMAIL`; run
   `git update-index --skip-worktree auth/oauth2-proxy/emails.txt` to
   suppress accidental staging.
7. `docker compose up -d caddy oauth2-proxy`.

## Edit the allowlist

Edit `auth/oauth2-proxy/emails.txt`, then `docker compose restart
oauth2-proxy`. Sessions for removed emails stay valid until cookie
expiry (24h max); to force-invalidate, rotate the cookie secret (below).

## Cookie / session rotation

```
NEW_SECRET=$(LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 32)
# set OAUTH2_PROXY_COOKIE_SECRET in .env to $NEW_SECRET
docker compose restart oauth2-proxy
```
Rotating the secret invalidates every session — everyone re-authenticates.

## Tailscale cert renewal

Tailscale certs expire ~every 90 days. Renew:
```
tailscale cert \
  --cert-file auth/caddy/certs/tailnet.crt \
  --key-file  auth/caddy/certs/tailnet.key \
  ordo.<tailnet>.ts.net
docker compose restart caddy
```
A monthly cron (`0 4 1 * *`) running the above stays ahead of expiry.

## Recovery — Google OIDC outage

When Google sign-in is unreachable, all browser paths fail. Two levers:

1. **Bearer fallback (break-glass, opt-in).** The dashboard's
   `_verify_auth()` retains an optional bearer path, disabled by default
   (`DASHBOARD_AUTH_TOKEN` unset, `AUTH_REQUIRED=False`) since the Caddy
   edge is the sole gate. As an emergency: set `DASHBOARD_AUTH_TOKEN` in
   `.env`, restart the dashboard, temporarily re-enable its host-port
   publish, and reach it directly:
   ```
   curl -H "Authorization: Bearer $DASHBOARD_AUTH_TOKEN" \
     http://localhost:8080/api/...
   ```
   Revert (unset the token, remove the host-port publish) once sign-in
   is restored.
2. **Direct container access.** `docker exec` runs any verb inside a
   service container while public access is broken.

## Recovery — oauth2-proxy crash

`docker compose restart oauth2-proxy` (Caddy's `forward_auth` retries
automatically). If it's unhealthy on boot, check
`docker logs ordo-oauth2-proxy-1` — the most common cause is an
`OAUTH2_PROXY_COOKIE_SECRET` that isn't exactly 16/24/32 bytes.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser stuck redirecting | Cookie domain mismatch | Confirm `CADDY_TAILNET_DOMAIN` matches `<tailnet>.ts.net` exactly |
| `redirect_uri_mismatch` from Google | OAuth client redirect URI doesn't match | Update the GCP console redirect URI to match `CADDY_TAILNET_HOSTNAME` |
| 502 from Caddy on a UI port | Service not on `ordo-net` (the single rendered network) or its container is down | `docker compose up -d --force-recreate <svc>`; NEVER recreate caddy with `--no-deps` (it orphans the netns members) |
| An SSO-gated port returns an empty 202 instead of the UI | Caddyfile uses `handle` (terminal) instead of `route` for SSO + reverse_proxy | Wrap `forward_auth` (`sso_forward_auth`) and the `reverse_proxy`/`handle_path` blocks in one `route { … }` so the request continues past forward_auth on 2xx |
| Any Google account signs in despite `emails.txt` | oauth2-proxy started with both `--email-domain=*` and `--authenticated-emails-file=…` (OR'd; wildcard wins) | Remove `--email-domain=*`; the file is then the only gate |
| `cookie_secret must be 16, 24, or 32 bytes` | Used `openssl rand -base64 32` (44 chars) | Use `tr -dc 'a-zA-Z0-9' </dev/urandom \| head -c 32` for exactly 32 raw bytes |
| `/n8n/webhook/...` (on `:443`) 302s to oauth2-proxy instead of proxying | The `:443` site's `handle /n8n/webhook/*` / `handle /n8n/rest/oauth2-credential/callback*` passthroughs are missing or shadowed | Ensure those `handle` blocks (each `uri strip_prefix /n8n` → `reverse_proxy n8n:5678`) precede the catch-all SSO `route {}`, and the legacy-redirect matcher excludes both paths (Caddy sorts `redir` before `handle`) |
| Caddy unhealthy, logs ok | Healthcheck targets `http://localhost/healthz` (port 80) | Caddyfile must include the `:80` site block that answers `/healthz` |
