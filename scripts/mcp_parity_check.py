#!/usr/bin/env python3
"""Compare the LiteLLM /mcp tool list against the pre-migration baseline.

Usage (from the worktree root, with the gateway reachable):
    python scripts/mcp_parity_check.py --url http://localhost:11435/mcp --key "$LITELLM_MASTER_KEY" \
        --baseline tests/fixtures/mcp_tool_baseline_2026-09-11.json --servers codebase-memory,comfyui,... \
        --require get_system_stats,get_queue,...

LiteLLM namespaces tools as `<server_id>-<tool>`; the baseline (Docker gateway) had bare tool
names. `compare` strips the prefix and asserts the current set is a SUPERSET of the baseline
plus every `required_extra` tool (the tools of the servers that were down when the baseline
was captured). Exit 0 when ok, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def strip_prefix(name: str, server_ids: set[str]) -> str:
    """Remove `<server_id>-` when the name starts with a KNOWN server id; longest id wins."""
    for sid in sorted(server_ids, key=len, reverse=True):
        prefix = sid + "-"
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def compare(baseline_tools: list[str], current_tools: list[str], server_ids: set[str],
            required_extra: list[str]) -> dict:
    current_bare = {strip_prefix(n, server_ids) for n in current_tools}
    expected = set(baseline_tools) | set(required_extra)
    missing = sorted(expected - current_bare)
    extra = sorted(current_bare - expected)
    return {"missing": missing, "extra": extra, "ok": not missing,
            "baseline_count": len(set(baseline_tools)), "current_count": len(current_bare)}


def fetch_tools(url: str, key: str) -> tuple[list[str], dict]:
    """POST tools/list to a LiteLLM /mcp endpoint; return (tool names, server_outcomes)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "x-litellm-api-key": f"Bearer {key}",
    })
    raw = urllib.request.urlopen(req, timeout=120).read().decode()
    messages = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            messages.append(json.loads(line[5:].strip()))
    if not messages:
        messages = [json.loads(raw)]
    result = next(m["result"] for m in messages if "result" in m)
    names = [t["name"] for t in result.get("tools", [])]
    outcomes = (result.get("_meta") or {}).get("litellm.ai/server_outcomes", {})
    return names, outcomes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--servers", required=True, help="comma-separated server ids")
    ap.add_argument("--require", default="", help="comma-separated bare tool names that must also be present")
    args = ap.parse_args(argv)
    baseline = json.load(open(args.baseline, encoding="utf-8"))
    baseline_tools = [t["name"] for t in baseline["tools"]]
    server_ids = {s for s in args.servers.split(",") if s}
    required = [t for t in args.require.split(",") if t]
    current, outcomes = fetch_tools(args.url, args.key)
    rep = compare(baseline_tools, current, server_ids, required)
    rep["server_outcomes"] = outcomes
    print(json.dumps(rep, indent=1))
    return 0 if rep["ok"] and all(o.get("status") == "ok" for o in outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
