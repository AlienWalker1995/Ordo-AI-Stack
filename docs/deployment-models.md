# Access & Deployment Models

How you reach Ordo's UIs is a **swappable edge layer**, not a property of the stack itself. The
render substrate (`ordo.yaml` → `out/`) produces the *same* set of services every time; what changes
between deployments is only **the front door in front of them**. Two facts make this possible:

- **Caddy is the only service that publishes host ports.** Every UI (`open-webui`, `dashboard`,
  `n8n`, `comfyui`, `hermes-dashboard`, `codebase-memory-ui`) and every gateway (`model-gateway`,
  `mcp-gateway`, `qdrant`, …) publishes **no** host port — each is reachable only on the internal
  `ordo-net` network, or *through* Caddy. So changing how the world reaches the stack means changing
  exactly one thing: Caddy's listeners and their TLS/DNS.
- **The edge is a plugin.** Caddy + oauth2-proxy ship as the opt-in [`edge`](../services/edge/plugin.yaml)
  plugin (compose profile `edge`), and the clean per-service hostnames ship as a *second* plugin,
  [`tailnet-names`](../services/tailnet-names/plugin.yaml). Picking a deployment model = choosing and
  adapting those two plugins (and the [`Caddyfile`](../auth/caddy/Caddyfile) they mount). The
  scheduler, agents, model gateway, and every UI container are byte-identical across all three models
  below.

All three models keep the **same security spine**: Google SSO (oauth2-proxy forward-auth) at the
edge, gating every browser-reachable UI against the `auth/oauth2-proxy/emails.txt` allowlist. What
differs between them is **exposure** — and public exposure *adds* hardening requirements, it never
removes the SSO gate. See [Security note](#security-note) below.

> **Implementation status, up front.** Only **Model 1 (Tailscale)** is wired and running today. Models
> 2 and 3 are **documented paths, not shipped defaults** — each section labels precisely what already
> exists versus what an implementer would still need to add, and *where* (which plugin/file) it plugs
> in. Nothing here invents a Caddyfile snippet or env key that isn't in the repo.

## Comparison at a glance

| | Model 1 — Tailscale | Model 2 — Self-hosted domain | Model 3 — Cloud VM (AWS/equiv.) |
|---|---|---|---|
| **Exposure** | Private tailnet only; zero public surface | Public internet (your NAT/router or tunnel) | Public internet (cloud provider network) |
| **DNS** | MagicDNS (`host.tailnet.ts.net` + `chat\|dash\|n8n\|comfy\|hermes\|graph.<tailnet>.ts.net`) | Your domain — public A/AAAA or split-horizon (`ordo.example.com`) | Route 53 / any DNS → the VM's elastic IP |
| **Cert mechanism** | `tailscale cert` (front door) + per-node MagicDNS certs via `tailscale serve` (sidecars); both auto-renew | Wildcard `*.example.com` via ACME **DNS-01** (no inbound `:80` needed) | ACM behind an ALB, **or** Caddy DNS-01 on the instance |
| **NAT / tunnel** | None — WireGuard mesh | Port-forward `:443` (+`:8443`–`:8448`) **or** an outbound tunnel | Security group allowing inbound `:443` only; ALB optional |
| **SSO gate** | Google SSO at the edge | Google SSO at the edge (unchanged) | Google SSO at the edge (unchanged) |
| **Implemented?** | ✅ **Yes — current default** | ⚠️ Documented path, not wired | ⚠️ Documented path, not wired |

---

## Model 1 — Tailscale tailnet (current default) ✅

The shipped deployment. The stack is reachable **only** from devices on the operator's Tailscale
tailnet; there is no public DNS record and no port open to the internet.

### What it gives you

- **Zero public exposure.** Traffic between tailnet devices is WireGuard-encrypted end to end; Caddy
  binds to the tailnet interface (`CADDY_BIND` = the host's `tailscale ip -4`, or a deliberate
  `0.0.0.0` that stays internet-dark because nothing is port-forwarded — operator-approved
  2026-07-17).
- **The SSO-gated port layer** — Caddy publishes `:443` (front door: landing page, the one Google
  OAuth callback, `/llm/*` and `/mcp` API routes, n8n webhook/OAuth passthroughs, legacy-subpath
  302s) plus one dedicated SSO-gated port per UI: `:8443` Open WebUI, `:8444` Dashboard (+`/grafana/`),
  `:8445` n8n, `:8446` ComfyUI, `:8447` Hermes, `:8448` codebase-memory. Every prebuilt SPA is served
  at the root it was compiled for. (See [operator-guide](operator-guide.md) and
  [configuration → Network Ports](configuration.md#network-ports).)
- **Clean per-service hostnames** via the [`tailnet-names`](../services/tailnet-names/plugin.yaml)
  plugin: one minimal Tailscale sidecar per UI joins the tailnet as its own node —
  `chat|dash|n8n|comfy|hermes|graph.<tailnet>.ts.net` — and `tailscale serve` forwards its clean host
  into the *existing* SSO-gated Caddy port over loopback (`network_mode: service:caddy`). So
  `https://chat.<tailnet>.ts.net/` and `https://host.tailnet.ts.net:8443/` are the same Open WebUI
  behind the same gate.
- **Auto-issued, auto-renewed TLS.** The front-door cert comes from `tailscale cert` (mounted at
  `auth/caddy/certs/tailnet.{crt,key}` via `TAILSCALE_CERT_DIR`; renew every ~90 days — see
  [auth runbook](runbooks/auth.md#tailscale-cert-renewal)). Each sidecar node terminates TLS with its
  own MagicDNS cert, issued and renewed automatically by `tailscale serve` — no operator action.

### What it requires

- A Tailscale tailnet, with the host and every client device joined.
- A **reusable auth key tagged `tag:ordo-edge`** (with a matching `tagOwners` entry in the tailnet
  ACL) in `TS_AUTHKEY`, SOPS-backed in `out/secrets.env` (this repo is public — never inline it). Each
  sidecar consumes ~1 of the plan's 50 free tagged resources.
- A Google OAuth 2.0 Web client with **exactly one** redirect URI —
  `https://host.tailnet.ts.net/oauth2/callback` on `:443`. One domain-scoped oauth2-proxy cookie plus
  one wildcard `--whitelist-domain=.${CADDY_TAILNET_DOMAIN}` covers every port *and* every sidecar
  name, so no per-port or per-name redirect URIs are needed. (The gate builds `rd=` from `{host}`
  — portless — precisely so the sidecar clean names sign in correctly; see the
  `(sso_forward_auth)` note in the [Caddyfile](../auth/caddy/Caddyfile).)
- Env identity in `ordo.yaml`'s `site:` block: `CADDY_TAILNET_HOSTNAME`, `CADDY_TAILNET_DOMAIN`,
  `CADDY_BIND` (see the commented keys in [`ordo.example.yaml`](../ordo.example.yaml)); secrets
  `OAUTH2_PROXY_CLIENT_ID` / `_SECRET` / `_COOKIE_SECRET`, `MCP_GATEWAY_TOKEN`, `TS_AUTHKEY`.

Full one-time setup: [docs/runbooks/auth.md](runbooks/auth.md) and
[docs/runbooks/secrets.md](runbooks/secrets.md).

### Tradeoffs

- **Access is gated on tailnet membership.** Anyone you want to reach the stack must be on the
  tailnet — great for a single operator + a small trusted circle, unsuitable for anonymous public
  users.
- **`tailscale cert` renews on a ~90-day clock** and is the one manual maintenance item; the auth
  runbook suggests a monthly cron. The sidecar certs, by contrast, are fully hands-off.

---

## Model 2 — Self-hosted on a real domain ⚠️ *documented path, not wired*

Same compose stack, same Google SSO edge, but the front door answers on a **public domain you own**
(`ordo.example.com`) instead of a MagicDNS name. Use this when you need URLs reachable without tailnet
membership.

### What it gives you

- Public, memorable URLs on your own domain, still fronted by the identical Caddy + oauth2-proxy edge
  and the same email allowlist.
- Freedom from tailnet membership as an access precondition — any device with the URL and an
  allowlisted Google account can sign in.

### What it requires

- **Public DNS.** A/AAAA records for `ordo.example.com` (and, if you want per-service hostnames like
  the tailnet sidecars, `chat.example.com`, … — or a wildcard `*.example.com`). Split-horizon DNS is
  an option if you want the name to resolve to a private address on your LAN and a public one outside.
- **A wildcard TLS cert via ACME DNS-01.** DNS-01 lets Caddy prove domain control by writing a TXT
  record through your DNS provider's API, so it can issue `*.example.com` **without any inbound `:80`**
  reaching the host. *This is the piece that does not exist in the repo today:* the shipped
  `caddy:2.11.4-alpine` image in [`services/edge/plugin.yaml`](../services/edge/plugin.yaml) has **no
  DNS-provider module**, and the [Caddyfile](../auth/caddy/Caddyfile) runs with `auto_https off` and a
  static `tls /etc/caddy/certs/tailnet.{crt,key}` mount.
  - **Requires:** a custom Caddy build (e.g. `xcaddy` with `caddy-dns/<provider>`) swapped into the
    `edge` plugin's `caddy.image`, DNS-provider API credentials as a new secret, and a Caddyfile
    change from `auto_https off` + static `tls` to either Caddy-managed DNS-01 issuance
    (`tls { dns <provider> … }`) or a mounted wildcard cert. Describe-not-implement: no such build,
    credential, or `tls` block is in the tree.
- **Inbound reachability** — either NAT/port-forward `:443` (plus `:8443`–`:8448` if you keep the
  port-per-service layout) from your router to the host, **or** an outbound tunnel (e.g. a
  cloudflared/Tailscale-Funnel-style connector) so no inbound firewall hole is opened.
- **A new Google OAuth redirect URI** — `https://ordo.example.com/oauth2/callback` — added to the
  same OAuth client.

### What changes from the default

| Piece | Model 1 (today) | Model 2 |
|---|---|---|
| `CADDY_BIND` | tailnet IP (or dark `0.0.0.0`) | `0.0.0.0` — now genuinely public-facing |
| `CADDY_TAILNET_HOSTNAME` / `_DOMAIN` | `host.tailnet.ts.net` / `tailnet.ts.net` | your real host/domain (the Caddyfile site block `{$CADDY_TAILNET_HOSTNAME}` and the oauth2-proxy `--whitelist-domain=.${CADDY_TAILNET_DOMAIN}` / `--cookie-domain` reuse these keys verbatim) |
| Cert source | `tailscale cert` + `serve` MagicDNS certs | ACME DNS-01 wildcard (custom Caddy build) |
| `auto_https` | `off` (static `tls` mount) | managed issuance or mounted wildcard |
| `tailnet-names` plugin | 6 MagicDNS sidecars | not applicable — replaced by public/wildcard DNS |
| Google redirect URI | one, on the tailnet `:443` | one, on `https://ordo.example.com/oauth2/callback` |

### Tradeoffs

- **You are now on the public internet.** The SSO gate is unchanged, but it is now the *only* thing
  between the world and your stack — see [Security note](#security-note). Fail-closed behavior,
  rate-limiting at the edge, and never letting a UI service publish its own host port become
  load-bearing rather than nice-to-have.
- **More moving parts to maintain:** a custom Caddy build to track, DNS-provider credentials to
  rotate, and NAT/tunnel plumbing — versus Tailscale's near-zero cert/DNS maintenance.

---

## Model 3 — Cloud VM (AWS or equivalent) ⚠️ *documented path, not wired*

Run the **same rendered compose stack** on a cloud VM instead of homelab hardware. The edge is
identical to Model 2 (public domain + Google SSO); only the *host* and the surrounding cloud
primitives differ.

### What it gives you

- The stack running on always-on cloud infrastructure, reachable on a public domain, still behind the
  same Google SSO edge and email allowlist.
- Cloud-native TLS and network controls (managed certs, security groups, load balancers) if you want
  them instead of self-managing at the Caddy layer.

### What it requires

- **A cloud VM** running Docker + the rendered `out/` stack, exactly as on a homelab host. `BASE_PATH`
  and the data roots point at the VM's disk.
- **DNS** — Route 53 (or any provider): an A record for `ordo.example.com` → the instance's elastic
  IP (or the load balancer).
- **A cert**, via one of:
  - **ACM + ALB** — an Application Load Balancer terminates TLS with an ACM cert and forwards to
    Caddy; Caddy can then run plain HTTP behind the ALB. (Caddy stays the SSO/routing layer; the ALB
    is just TLS + inbound.)
  - **Caddy DNS-01 on the instance** — the Route 53 DNS-provider variant of Model 2's custom Caddy
    build, issuing the wildcard directly on the VM (no ALB).
- **Security groups locking inbound to `:443`** (plus `:8443`–`:8448` only if you expose the ports
  directly rather than fronting everything through an ALB path). Everything else stays on the internal
  Docker network, unpublished.
- The **same Google OAuth redirect URI** as Model 2 (`https://ordo.example.com/oauth2/callback`).

### GPU-service caveat

The compose stack splits cleanly by hardware need:

- **GPU-only services** — `comfyui`, `voice` (STT/TTS), `song-gen`, `ltx-trainer` — self-declare
  `nvidia: true` in their plugin manifests and require a **GPU instance** (e.g. AWS `g`/`p` families
  with the NVIDIA container runtime). On a CPU-only VM the render engine gates them **off** rather than
  shipping a guaranteed crash.
- **The CPU-only subset** — llama.cpp (CPU inference), Open WebUI, n8n, dashboard, the model/MCP
  gateways, Qdrant, the edge — **runs anywhere**, including a plain CPU VM. A perfectly valid cloud
  deployment is the CPU subset in the cloud with no GPU services at all.

### What changes from the default

Everything in Model 2's change table, plus: the host is a cloud VM (not homelab hardware); TLS may
move to ACM/ALB; and inbound is governed by cloud security groups instead of a home router's
port-forward. *Describe-not-implement:* there is **no** cloud IaC, ALB/ACM wiring, or security-group
definition in the repo — this section is the shape an implementer would build, reusing the identical
`edge` plugin and Caddyfile.

### Tradeoffs

- **Cloud cost and public exposure** — an always-on GPU instance is expensive, and the stack is on
  the public internet with the same hardening obligations as Model 2.
- **Local-first is lost** — the design center of Ordo is local-first, offline-capable inference on the
  operator's own hardware. A cloud VM trades that away for reachability and uptime; pick it only when
  that trade is deliberate.

---

## Choosing a model

- **Just you (and a few trusted people), private, minimal maintenance → Model 1 (Tailscale).** The
  shipped default. Nothing is publicly exposed, certs are near-hands-off, and setup is one Google
  OAuth client + one tailnet auth key.
- **You need URLs reachable without tailnet membership, on hardware you already run → Model 2
  (self-hosted domain).** Accept the public-internet hardening burden and the custom DNS-01 Caddy
  build.
- **You need always-on uptime or don't have suitable local hardware → Model 3 (cloud VM).** Same edge
  as Model 2, on rented infrastructure; mind the GPU-instance cost and the loss of local-first.

When in doubt, stay on Model 1 — it is the only model that is exposure-free by construction.

## Security note

All three models keep **Google SSO at the edge** as the sole browser-auth gate; none of them weakens
that posture. The difference is exposure, and the public models (2 and 3) *add* obligations:

- **The SSO gate must fail closed.** oauth2-proxy forward-auth returning anything but a clean 2xx must
  deny, never fall through. The email allowlist (`auth/oauth2-proxy/emails.txt`) is the authorization
  boundary — never widen it with `--email-domain=*` (a documented footgun: it ORs with the file and
  the wildcard wins — see [auth runbook troubleshooting](runbooks/auth.md#troubleshooting)).
- **No UI service ever publishes its own host port.** Caddy is the *only* publisher, in every model.
  A service that publishes its own port bypasses the SSO gate entirely — this invariant is guarded by
  `tests/test_caddyfile_invariants.py` and the `ordo/compose.py` no-host-port rule, and it must hold
  on a public deployment above all.
- **The `CADDY_BIND` lesson.** The rendered compose uses `${CADDY_BIND:?…}`, which refuses an
  **empty/unset** value — catching the historical case where an empty bind silently degraded to
  `0.0.0.0:443` with no operator intent. It does **not** police the *value*: on Model 1, `0.0.0.0`
  stays internet-dark only because nothing is port-forwarded; on Models 2/3, binding `0.0.0.0` is
  genuinely public and must be a deliberate, hardened choice.
- **Public models should add edge rate-limiting** (and consider fail2ban-style abuse controls) in
  front of the sign-in flow — an obligation that simply doesn't arise on a private tailnet.
- **Secrets stay host-side.** SOPS + age at rest; plaintext only in gitignored `out/secrets.env`,
  never in the repo or a container image. This is unchanged across models — but a public deployment
  raises the stakes of getting it wrong. See [SECURITY.md](../SECURITY.md) and
  [docs/runbooks/secrets.md](runbooks/secrets.md).

## See also

- [Operator guide](operator-guide.md) — render engine, bring-up, day-2 operations
- [Getting started](GETTING_STARTED.md) — common workflows and the Tailscale + SSO walkthrough
- [Auth front door runbook](runbooks/auth.md) — one-time SSO setup, cert renewal, troubleshooting
- [Secrets runbook](runbooks/secrets.md) — SOPS + age, `secrets.env` model
- [Configuration](configuration.md#network-ports) — env vars and the port-per-service layout
- Plugins: [`edge`](../services/edge/plugin.yaml) (Caddy + oauth2-proxy) ·
  [`tailnet-names`](../services/tailnet-names/plugin.yaml) (per-service sidecars) ·
  [`Caddyfile`](../auth/caddy/Caddyfile) (the only host-port publisher)
