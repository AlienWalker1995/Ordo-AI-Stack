#!/usr/bin/env python3
"""Validate openclaw.json for Ordo AI Stack conventions (gateway provider, M7).

Exit 0 if valid or if config path is missing and not explicitly required.
Exit 1 on validation errors or unreadable JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _default_config_path() -> Path:
    env = os.environ.get("OPENCLAW_CONFIG_PATH", "").strip()
    if env:
        return Path(env)
    root = os.environ.get("ORDO_AI_STACK_ROOT", "").strip() or os.environ.get("AI_TOOLKIT_ROOT", "").strip()
    base = Path(root).resolve() if root else None
    if base and base.is_dir():
        return base / "data" / "openclaw" / "openclaw.json"
    # Repo layout: scripts/.. /data/openclaw/openclaw.json
    here = Path(__file__).resolve().parent
    return (here.parent / "data" / "openclaw" / "openclaw.json").resolve()


def validate(data: dict) -> list[str]:
    """Return list of error strings (empty if ok)."""
    errs: list[str] = []
    models = data.get("models")
    if not isinstance(models, dict):
        errs.append("models must be an object")
        return errs
    providers = models.get("providers")
    if not isinstance(providers, dict):
        errs.append("models.providers must be an object")
        return errs
    gw = providers.get("gateway")
    if not isinstance(gw, dict):
        errs.append("models.providers.gateway must exist (OpenClaw routes via model-gateway)")
        return errs
    base = str(gw.get("baseUrl", "")).strip()
    if not base:
        errs.append("gateway.baseUrl is required")
    elif "/v1" not in base:
        errs.append(f"gateway.baseUrl should include /v1 OpenAI-compatible path: {base!r}")
    if (
        "model-gateway" not in base
        and "11435" not in base
        and "localhost" not in base
        and "127.0.0.1" not in base
    ):
        errs.append(
            f"gateway.baseUrl should target model-gateway (e.g. http://model-gateway:11435/v1): {base!r}"
        )
    if gw.get("api") != "openai-responses":
        errs.append(f'gateway.api should be "openai-responses" (got {gw.get("api")!r})')
    headers = gw.get("headers")
    if isinstance(headers, dict) and headers.get("X-Service-Name") != "openclaw":
        errs.append('gateway.headers["X-Service-Name"] should be "openclaw" for telemetry routing')
    return errs


def main() -> int:
    p = argparse.ArgumentParser(description="Validate openclaw.json for Ordo AI Stack gateway wiring.")
    p.add_argument(
        "config",
        nargs="?",
        default=None,
        help="Path to openclaw.json (default: OPENCLAW_CONFIG_PATH or data/openclaw/openclaw.json)",
    )
    p.add_argument(
        "--require",
        action="store_true",
        help="Fail if the config file does not exist",
    )
    args = p.parse_args()
    path = Path(args.config).resolve() if args.config else _default_config_path()

    if not path.exists():
        if args.require:
            print(f"validate_openclaw_config: missing required file: {path}", file=sys.stderr)
            return 1
        print(f"validate_openclaw_config: skip (no file): {path}")
        return 0

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"validate_openclaw_config: invalid JSON or read error: {e}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print("validate_openclaw_config: root must be a JSON object", file=sys.stderr)
        return 1

    errs = validate(data)
    if errs:
        print(f"validate_openclaw_config: {path}:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"validate_openclaw_config: OK {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
