"""The three stdio->HTTP bridge images must agree on their bridge pins.

codebase-memory, memory-vault and n8n each wrap an stdio MCP upstream in mcp-proxy inside their own
image (the gateway never spawns a process). They run the same bridge, so a version that drifts in
one Dockerfile is a bridge behaving differently on one server only, which is exactly the kind of
difference nothing else in the stack would surface.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRIDGES = ("codebase-memory", "memory-vault", "n8n")

MCP_PROXY_ARG = re.compile(r"^ARG MCP_PROXY_VERSION=(\S+)", re.MULTILINE)
MCP_PIN = re.compile(r'"mcp==([^"]+)"')


def _pins(service: str) -> tuple[str, str]:
    text = (ROOT / "services" / service / "Dockerfile").read_text(encoding="utf-8")
    proxy = MCP_PROXY_ARG.search(text)
    mcp = MCP_PIN.search(text)
    assert proxy, f"{service}/Dockerfile: no `ARG MCP_PROXY_VERSION=<version>`"
    assert mcp, f'{service}/Dockerfile: no `"mcp==<version>"` pin'
    return proxy.group(1), mcp.group(1)


def test_every_bridge_image_pins_both_versions_exactly():
    for service in BRIDGES:
        proxy, mcp = _pins(service)
        for label, value in (("MCP_PROXY_VERSION", proxy), ("mcp", mcp)):
            assert re.fullmatch(r"\d+\.\d+\.\d+", value), f"{service}: {label} is not an exact version ({value})"


def test_the_bridges_agree_on_mcp_proxy_and_mcp_versions():
    pins = {service: _pins(service) for service in BRIDGES}
    assert len(set(pins.values())) == 1, f"bridge pins disagree: {pins}"
