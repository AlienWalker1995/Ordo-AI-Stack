# Optional: document SSRF egress blocking on Windows.
# This script BLOCKS NOTHING. On Docker Desktop (WSL2 backend) the engine runs in
# Docker Desktop's own docker-desktop distro: its DOCKER-USER chain is not reachable
# from Windows, and iptables run in a separate user WSL distro land in that distro's
# network namespace, not the engine's. Egress-capable MCP servers are unfiltered here.
#
# See the "SSRF Defenses (MCP)" section of
# docs/product requirements docs/security-and-trust-model.md.

$doc = @"
SSRF egress blocking (Windows / Docker Desktop)
==============================================

Docker Desktop on Windows does not expose the DOCKER-USER iptables chain from
the host. Options:

Nothing is blocked on this host. MCP servers declaring network: stack
(searxng, qdrant-rag, n8n, orchestration, comfyui-mcp) can reach every stack
service, the LAN, the tailnet and the internet.

Running scripts/ssrf-egress-block.sh from a user WSL distro does NOT help: its
iptables are that distro's, not Docker Desktop's engine.

What you can do today:
1. Drop MCP plugins you do not use from plugins: in ordo.yaml, then ordo apply.
2. Keep the tailnet ACLs and LAN firewall tight: they are the only filter.

The platform-independent fix (per-server internal networks, an egress proxy
for searxng) is tracked in the "SSRF Defenses (MCP)" section of
docs/product requirements docs/security-and-trust-model.md
"@
Write-Host $doc
