"""Append-only JSONL audit log with size-based rotation.

One privileged call -> one record -> one fsync'd JSONL line. ops-controller writes a record for
every state-changing call it receives (ControlPlane.handle), so the log shows which verbs were
asked for, by whom, and how each ended. The log is bounded: when the live file reaches
`max_bytes` it becomes generation 1 (`audit.log` -> `audit.1.log`), older generations shift up,
and only `backups` of them are kept.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 5


class AuditLog:
    """Append-only JSONL audit log with size-based rotation.

    Thread-safe; rotation is checked on each write. The directory is created on the first write,
    never on read, so a control plane that has done nothing privileged leaves no files behind.
    """

    def __init__(self, path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES, backups: int = DEFAULT_BACKUPS):
        if max_bytes <= 0 or backups < 1:
            raise ValueError("an audit log needs max_bytes > 0 and at least one backup generation")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()

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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                self._rotate()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        return rec

    def generation(self, n: int) -> Path:
        """The file holding generation `n` (0 = the live file): `audit.log` -> `audit.<n>.log`."""
        if n == 0:
            return self.path
        return self.path.with_name(f"{self.path.stem}.{n}{self.path.suffix}")

    def _rotate(self) -> None:
        oldest = self.generation(self.backups)
        if oldest.exists():
            oldest.unlink()
        for n in range(self.backups - 1, -1, -1):
            current = self.generation(n)
            if current.exists():
                current.rename(self.generation(n + 1))

    def tail(self, limit: int) -> list[dict[str, Any]]:
        """The newest `limit` records, newest first, read across the kept generations.

        A line that is not valid JSON (a torn write) is skipped rather than failing the read.
        """
        entries: list[dict[str, Any]] = []
        with self._lock:
            for n in range(self.backups + 1):
                wanted = limit - len(entries)
                if wanted <= 0:
                    break
                path = self.generation(n)
                if not path.exists():
                    continue
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = deque((line for line in f if line.strip()), maxlen=wanted)
                for line in reversed(lines):
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries[:limit]
