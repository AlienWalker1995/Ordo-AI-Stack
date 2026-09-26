# SSO Front Door — Operator Runbook

## Setup checklist (one-time)

1. Google Cloud Console → create an OAuth 2.0 Web client.
   - Authorized origin: `https://ordo.<tailnet>.ts.net`
   - Authorized redirect: `https://ordo.<tailnet>.ts.net/oauth2/callback`
   - This single `:443` redirect URI covers the whole stack: the
     port-per-service model (`:443` front door plus `:8443`–`:8450`)
     shares one domain-scoped oauth2-proxy cookie — no per-port URIs.
2. Turn it on (prompts for the tailnet hostname, the Caddy bind address,
   the client ID + secret and the allowlisted emails; `--yes` takes them
   from flags, and the secret from `OAUTH2_PROXY_CLIENT_SECRET`):
   ```
   ordo remote enable
   ```
   It writes the `CADDY_*` keys and the allowlist (`SSO_ALLOWED_EMAILS`)
   under `site:` in `out/ordo.yaml`, the client pair into the secret
   store (the cookie secret is generated) and materializes
   `out/secrets.env` ([secrets runbook](secrets.md)), re-renders (which
   writes `out/oauth2-proxy/emails.txt`), and offers to issue the
   Tailscale cert into `auth/caddy/certs/`. Use your tailnet IP
   (`tailscale ip -4`) as the bind to restrict Caddy to the tailnet, or
   `0.0.0.0` for all interfaces (LAN included).
3. `ordo up --all` (from the repo root). The loopback UI ports close and
   Caddy becomes the one front door.

`ordo remote disable` reverses all of it (UIs back on `127.0.0.1`).

## Local mode: dashboard sign-in without the edge

With the edge off there is no SSO identity, so the dashboard
(`http://127.0.0.1:8444`) takes a local sign-in instead. The render
passes `DASHBOARD_LOCAL_LOGIN_TOKEN` to the dashboard only (and requires
it in the secret store, where `ordo init` generates it); with the edge
on, no service receives it and the sign-in route answers 404.

- `ordo up` prints `http://127.0.0.1:8444/#sign-in=<token>`. The token
  is in the URL fragment, which the browser never sends to the server;
  the page posts it once for an HttpOnly, SameSite=Strict session cookie
  (30 days). Or paste the token into the page's sign-in prompt.
- An install made before this secret existed gets it minted by its next
  `ordo up`.
- Revoke every session: `ordo secrets rotate DASHBOARD_LOCAL_LOGIN_TOKEN`, then
  `ordo recreate dashboard`. Sessions are signed with a key derived from
  the token, so old cookies stop working.
- Other containers on the stack network cannot use it: they hold neither
  the token nor a cookie minted from it, so their unauthenticated calls
  still get 401 (the confused-deputy guarantee of `dashboard/auth.py`).

## Edit the allowlist

Edit `SSO_ALLOWED_EMAILS` (comma-separated) under `site:` in
`out/ordo.yaml` (or run `ordo remote enable`), then `ordo apply`: the
render rewrites `out/oauth2-proxy/emails.txt` and, because oauth2-proxy
carries the list's digest, recreates it. Sessions for removed emails stay valid until cookie
expiry (24h max); to force-invalidate, rotate the cookie secret (below).

## Cookie / session rotation

```
ordo secrets rotate OAUTH2_PROXY_COOKIE_SECRET    # writes the store, materializes out/secrets.env
ordo recreate --reading OAUTH2_PROXY_COOKIE_SECRET
```
A plain container restart keeps the old environment, so the new secret only loads on a recreate.
Rotating the secret invalidates every session: everyone re-authenticates.

## Tailscale cert renewal

Tailscale certs expire ~every 90 days. Renew:
```
tailscale cert \
  --cert-file auth/caddy/certs/tailnet.crt \
  --key-file  auth/caddy/certs/tailnet.key \
  ordo.<tailnet>.ts.net
ordo recreate caddy
```
Renewal is manual: no job in the repo, the Hermes cron or the host scheduler runs it. Check the
expiry with `openssl x509 -enddate -noout -in auth/caddy/certs/tailnet.crt` and renew a couple of
weeks ahead (note the date in a calendar). `ordo remote enable` offers the same `tailscale cert`
call with these file names.

Run the renewal from the repo root. hermes-dashboard and the tailnet sidecars share caddy's network
namespace, and `ordo recreate caddy` recreates them in the same call. The dashboard's Restart
button for caddy (ops-controller `POST /services/caddy/restart`) is also safe: it restarts the
members after caddy. Never a bare `docker restart ordo-caddy-1`: it leaves every member running
with no network interface.

## Recovery — Google OIDC outage

When Google sign-in is unreachable, all browser paths fail. Two levers:

1. **Bearer (break-glass).** The dashboard's protected routes accept
   `Authorization: Bearer <OPS_CONTROLLER_TOKEN>` (`dashboard/auth.py`).
   The dashboard has no host port, so call it from a container on
   `ordo-net` that holds the token:
   ```
   docker exec ordo-agent-1 sh -c 'curl -H "Authorization: Bearer $OPS_CONTROLLER_TOKEN" \
     http://dashboard:8080/api/...'
   ```
2. **Direct container access.** `docker exec` runs any verb inside a
   service container while public access is broken.

## Recovery — oauth2-proxy crash

`ordo recreate oauth2-proxy` from the repo root (Caddy's `forward_auth` retries
automatically). If it's unhealthy on boot, check
`docker logs ordo-oauth2-proxy-1` — the most common cause is an
`OAUTH2_PROXY_COOKIE_SECRET` that isn't exactly 16/24/32 bytes.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser stuck redirecting | Cookie domain mismatch | Confirm `CADDY_TAILNET_DOMAIN` matches `<tailnet>.ts.net` exactly |
| `redirect_uri_mismatch` from Google | OAuth client redirect URI doesn't match | Update the GCP console redirect URI to match `CADDY_TAILNET_HOSTNAME` |
| 502 from Caddy on a UI port | Service not on `ordo-net` (the single rendered network) or its container is down | `ordo recreate <svc>` (for caddy it recreates the netns members too, by name) |
| An SSO-gated port returns an empty 202 instead of the UI | Caddyfile uses `handle` (terminal) instead of `route` for SSO + reverse_proxy | Wrap `forward_auth` (`sso_forward_auth`) and the `reverse_proxy`/`handle_path` blocks in one `route { … }` so the request continues past forward_auth on 2xx |
| Any Google account signs in despite `emails.txt` | oauth2-proxy started with both `--email-domain=*` and `--authenticated-emails-file=…` (OR'd; wildcard wins) | Remove `--email-domain=*`; the file is then the only gate |
| `cookie_secret must be 16, 24, or 32 bytes` | Used `openssl rand -base64 32` (44 chars) | Use `tr -dc 'a-zA-Z0-9' </dev/urandom \| head -c 32` for exactly 32 raw bytes |
| `/n8n/webhook/...` (on `:443`) 302s to oauth2-proxy instead of proxying | The `:443` site's `handle /n8n/webhook/*` / `handle /n8n/rest/oauth2-credential/callback*` passthroughs are missing or shadowed | Ensure those `handle` blocks (each `uri strip_prefix /n8n` → `reverse_proxy n8n:5678`) precede the catch-all SSO `route {}`, and the legacy-redirect matcher excludes both paths (Caddy sorts `redir` before `handle`) |
| Caddy unhealthy, logs ok | Healthcheck targets `http://localhost/healthz` (port 80) | Caddyfile must include the `:80` site block that answers `/healthz` |
