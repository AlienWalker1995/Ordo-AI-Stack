"""The Overview's dismissed attention items, kept in the dashboard's data directory.

The file maps an attention item's id (console.build_attention) to the time it was dismissed. It
is small (one entry per condition a person chose to hide) and is pruned as conditions clear
(console.prune_dismissals), so it never grows without bound.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

FILE_NAME = "attention_dismissed.json"


def load(path: Path) -> dict[str, float]:
    """The dismissals on disk; an absent or unreadable file means none (and is logged)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.error("[attention] could not read %s: %s; treating it as empty", path, e)
        return {}
    if not isinstance(data, dict):
        logger.error("[attention] %s is not an object; treating it as empty", path)
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, int | float)}


def save(path: Path, dismissed: dict[str, float]) -> None:
    """Write atomically, so a crash mid-write leaves the previous file, never half of one."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dismissed, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
