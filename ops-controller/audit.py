"""Append-only JSONL audit log with size-based rotation.

One privileged call -> one record -> one fsync'd JSONL line.
Used by the Hermes-facing endpoints in ``main.py`` so we can see exactly
which container/compose verbs the agent triggered and when.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    """Append-only JSONL audit log with simple size-based rotation.

    One privileged call -> one record -> one fsync'd JSONL line.
    Thread-safe; rotation is opportunistic (checked on each write).
    """

    def __init__(self, path: str | Path, *, max_bytes: int = 50 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        action: str,
        target: str,
        result: str,
        caller: str,
        **extra: Any,
    ) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "ts": time.time(),
            "caller": caller,
            "action": action,
            "target": target,
            "result": result,
        }
        rec.update(extra)
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with self._lock:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                self._rotate()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        return rec

    def _rotate(self) -> None:
        # ``audit.jsonl`` -> ``audit.1.jsonl`` (insert generation before suffix)
        rolled = self.path.with_name(f"{self.path.stem}.1{self.path.suffix}")
        if rolled.exists():
            rolled.unlink()
        self.path.rename(rolled)
