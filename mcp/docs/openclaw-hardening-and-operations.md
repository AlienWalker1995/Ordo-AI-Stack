# Hardening MCP + making everything manageable from OpenClaw

This stack splits **security controls** from **agent capabilities**. You do not get “one magic MCP that does everything.” You get a **small set of patterns** that together let OpenClaw operate the whole toolkit **safely**.

---

## 1. What “hardening” means here

| Layer | What it does | Where to read more |
|--------|----------------|-------------------|
| **Network** | Keep **`mcp-gateway`** on the **backend** network by default; do not expose **8811** on the host unless you use `overrides/mcp-expose.yml` on purpose. | [docs/docker-runtime.md](../../docs/docker-runtime.md), [overrides/mcp-expose.yml](../../overrides/mcp-expose.yml) |
| **OpenClaw UI** | Bind Control UI appropriately; use gateway token; optional Tailscale / secure overlay. | [openclaw/OPENCLAW_SECURE.md](../../openclaw/OPENCLAW_SECURE.md) |
| **Egress (MCP containers)** | MCP-spawned containers can reach the internet; block metadata / private ranges at the host if you need defense-in-depth. | [docs/runbooks/SECURITY_HARDENING.md](../../docs/runbooks/SECURITY_HARDENING.md) §1 |
| **Docker socket** | Only **`mcp-gateway`** (and **`ops-controller`**) mount **`docker.sock`**. That is required for the gateway to spawn MCP servers; treat the host as in scope for compromise if those services are abused. | [SECURITY.md](../../SECURITY.md) |
| **Secrets** | API keys and tokens live in **`.env`**, not in committed JSON. **`OPS_CONTROLLER_TOKEN`**, **`DASHBOARD_AUTH_TOKEN`**, **`HF_TOKEN`**, etc., gate privileged actions. | [.env.example](../../.env.example) |
| **MCP policy** | **`data/mcp/registry.json`** (from **`mcp/registry.json.example`**) can express **`allow_clients`** per server (e.g. disable **`filesystem`** by default). | [SECURITY_HARDENING.md](../../docs/runbooks/SECURITY_HARDENING.md) §10 |

The **forked `openclaw-mcp-bridge`** improves **tool discovery and naming** in OpenClaw; it does **not** replace firewalling, network isolation, or token gates. Hardening is still **compose + host + secrets + policy**.

---

## 2. How OpenClaw “manages everything” (two layers)

MCP is designed for **tool calls** (search, run workflow, click browser, n8n tools). **Infra** actions (pull multi‑GB models, `pip` inside ComfyUI, restart containers) need **stronger** gates, so this repo routes them through **authenticated HTTP**, not only through raw MCP.

### Layer A — MCP gateway (`http://mcp-gateway:8811/mcp`)

- **One URL** in OpenClaw: **`plugins.entries["openclaw-mcp-bridge"].config.servers.gateway`**.
- Covers **catalog** behavior: DuckDuckGo, n8n, Tavily, ComfyUI MCP tools (`list_workflows`, `run_workflow`, …), etc.
- Use **`gateway__call`** with the **exact** inner `tool` name + `args`, or flat **`gateway__…`** tools when the bridge registers them.

### Layer B — Dashboard + ops-controller (privileged)

OpenClaw’s gateway container receives **`DASHBOARD_URL`** and **`DASHBOARD_AUTH_TOKEN`** (see **`docker-compose.yml`**). Agents use **`exec` / `wget` / `curl`** (per your workspace policy) to call the **dashboard** and **ops-controller** APIs for operations MCP does not own end‑to‑end:

| Need | Typical path |
|------|----------------|
| **Pull ComfyUI model packs** | `POST` dashboard **`/api/comfyui/pull`**, poll **`/api/comfyui/pull/status`** |
| **Download arbitrary weights** | `POST` **`/api/models/download`** or **`/api/models/pull`** (may proxy **ops-controller**) |
| **Install custom-node Python deps** | `POST` **`/api/comfyui/install-node-requirements`** (ops-controller runs **`pip` in the ComfyUI container**) |
| **Service restarts / Docker-backed ops** | **ops-controller** (token **`OPS_CONTROLLER_TOKEN`**) |

ComfyUI MCP tools such as **`install_custom_node_requirements`** / **`restart_comfyui`** (when exposed) still depend on **`OPS_CONTROLLER_TOKEN`** being set and passed through **`registry-custom`** — same trust boundary.

So **“entirely manageable via OpenClaw”** means: **MCP for interactive tools** + **documented dashboard/ops calls with auth** for **privileged** work — not a single MCP server that holds every capability without checks.

---

## 3. ComfyUI: workflows, models, nodes, monitoring

| Goal | How the agent does it |
|------|-------------------------|
| **List / run workflows** | MCP **`comfyui__…`** tools via **`gateway__call`**; prompts and overrides in **`args`**. |
| **Create / edit workflow files** | On the host: **`data/comfyui-workflows/`** (ComfyUI + comfyui-mcp). **Not** under **`data/openclaw/workspace/`** — agents use MCP tools; operators edit files on the host or via ComfyUI. |
| **Pull models** | Dashboard APIs above (**not** usually a single MCP “pull everything” tool). |
| **Custom nodes / pip** | Dashboard **`install-node-requirements`** or ComfyUI MCP **management** tools (if enabled), with **ops-controller** auth. |
| **Progress** | **Pull**: poll dashboard status endpoints. **Generation**: use **`run_workflow`** response; for long jobs, ComfyUI queue/history patterns as documented in **TROUBLESHOOTING** / Comfy docs (full “live progress streaming” may still need extra tooling). |

Details and pitfalls: [comfyui-openclaw.md](comfyui-openclaw.md), [docs/runbooks/TROUBLESHOOTING.md](../../docs/runbooks/TROUBLESHOOTING.md).

---

## 4. Other MCP services (n8n, Tavily, GitHub, …)

Same pattern:

- **Day‑to‑day tools** → **MCP gateway** + **`gateway__call`**.
- **Admin / account / credential setup** → often **outside** MCP (n8n UI, GitHub token in `.env`, etc.). The agent can **guide** or **automate via HTTP** where you expose a safe API and **tokens**.

Keep **`data/mcp/servers.txt`** aligned with what you actually want enabled; use **`registry.json`** to restrict risky servers (e.g. **filesystem**).

---

## 5. Making this stronger over time (optional hardening)

1. **Always set** **`DASHBOARD_AUTH_TOKEN`** and **`OPS_CONTROLLER_TOKEN`** in production and rotate on leak.
2. **Do not** expose **8811** publicly without TLS and client auth (or Tailscale only).
3. **Review** **`MCP_GATEWAY_SERVERS`** / **`servers.txt`** — fewer servers = smaller attack surface.
4. **Egress rules** for MCP subnets if you run untrusted prompts against tools that fetch URLs.
5. **Audit** **`data/ops-controller/audit.log`** for model pulls and pip installs.

If you later want **more** operations behind MCP only, the maintainable approach is usually a **small “dashboard MCP” adapter** (one MCP server that wraps existing authenticated routes), not giving the generic gateway unlimited docker or host access.

---

## Related

- [README.md](../README.md) — MCP module layout and OpenClaw plugin config  
- [comfyui-openclaw.md](comfyui-openclaw.md) — reliability and tool naming  
- [openclaw/workspace/TOOLS.md.example](../../openclaw/workspace/TOOLS.md.example) — short agent contract  
