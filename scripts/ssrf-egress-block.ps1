# Optional: document SSRF egress blocking on Windows.
# On Windows (Docker Desktop), the DOCKER-USER iptables chain is not directly
# accessible from the host. This script prints guidance; actual blocking
# would require WSL2 iptables, a proxy container, or Docker Desktop network policies.
#
# See the "SSRF Defenses (MCP)" section of
# docs/product requirements docs/security-and-trust-model.md.

$doc = @"
SSRF egress blocking (Windows / Docker Desktop)
==============================================

Docker Desktop on Windows does not expose the DOCKER-USER iptables chain from
the host. Options:

1. WSL2: If you run Docker via WSL2, run the Linux script from inside WSL:
   wsl -e bash -c 'cd /c/dev/ordo-ai-stack && ./scripts/ssrf-egress-block.sh --dry-run'
   Then apply from a WSL shell with sudo.

2. Docker Desktop network policies: Enterprise feature; see Docker docs.

3. Accept default posture: For local-only use, the risk is lower; ensure
   MCP tools are from trusted sources and only enable what you need.

Full details: docs/product requirements docs/security-and-trust-model.md
"@
Write-Host $doc
