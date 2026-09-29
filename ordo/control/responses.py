"""The payload conventions every ops-controller handler shares.

A handler returns a dict. A refusal or failure carries its HTTP status in-band as `_status`
(`error()`); `as_response()` pops it at the edge, and a payload without one is a 200. A
destructive verb runs only when the body says `"confirm": true` (`confirmed()`).
"""
from __future__ import annotations

from typing import Any

CONFIRM_REQUIRED = ('Destructive operation requires confirmation. Set {"confirm": true} in the request body '
                    "to proceed.")


def confirmed(body: dict[str, Any]) -> bool:
    """True only when the body says `"confirm": true` (JSON true). Any other value, "no" and "false"
    included, is not a confirmation: a truthiness check would let those run a destructive action."""
    return body.get("confirm") is True


def error(status: int, message: str, **extra: Any) -> dict[str, Any]:
    return {"_status": status, "error": message, **extra}


def as_response(payload: dict[str, Any]) -> tuple[int, dict]:
    status = int(payload.pop("_status", 200)) if isinstance(payload, dict) else 200
    return status, payload
