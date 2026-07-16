"""Lease history — the imperative shell's durable record of every GPU lease outcome.

The pure scheduler keeps only LIVE state (by design: tick-relative clock, no I/O). This sink
records the missing half — what ran, when, for how long, and how it ended — as append-only
JSONL that the control plane serves to the dashboard's orchestration tab. Wall-clock
timestamps are stamped HERE (the broker layer), keeping the decision core clock-free.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

MAX_RECORDS = 1000     # newest records kept when trimming
TRIM_THRESHOLD = 2000  # trim when the file grows past this many records


class LeaseHistory:
    def __init__(
        self,
        path: str | Path,
        now_fn: Callable[[], float] = time.time,
        max_records: int = MAX_RECORDS,
        trim_threshold: int = TRIM_THRESHOLD,
    ):
        self.path = Path(path)
        self.now_fn = now_fn
        self.max_records = int(max_records)
        self.trim_threshold = int(trim_threshold)
        self._pending: dict[str, dict] = {}  # id -> in-flight record (submitted, maybe started)

    # --- lifecycle (driven by the Broker) ---
    def submitted(self, job_id: str, kind: str, vram_gb: float) -> None:
        self._pending[job_id] = {
            "id": job_id,
            "kind": kind,
            "vram_gb": float(vram_gb),
            "submitted": self.now_fn(),
            "started": None,
        }

    def started(self, job_id: str) -> None:
        rec = self._pending.get(job_id)
        if rec and rec["started"] is None:
            rec["started"] = self.now_fn()

    def ended(self, job_id: str, outcome: str) -> None:
        rec = self._pending.pop(job_id, None)
        if rec is None:
            return  # unknown lease (e.g. controller restarted mid-flight) — nothing to record
        rec["ended"] = self.now_fn()
        rec["outcome"] = outcome
        self._append(rec)

    def rejected(self, job_id: str) -> None:
        self.ended(job_id, "rejected")

    # --- storage ---
    def _append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A crash mid-write can leave a torn line with no trailing newline; appending straight
        # onto it would corrupt THIS record too. Start on a fresh line if the file ends torn.
        needs_newline = False
        if self.path.exists() and self.path.stat().st_size > 0:
            with self.path.open("rb") as f:
                f.seek(-1, 2)
                needs_newline = f.read(1) != b"\n"
        with self.path.open("a", encoding="utf-8") as f:
            if needs_newline:
                f.write("\n")
            f.write(json.dumps(rec) + "\n")
        self._maybe_trim()

    def _read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn line (crash mid-write) must not poison the whole history
        return out

    def _maybe_trim(self) -> None:
        records = self._read_all()
        if len(records) <= self.trim_threshold:
            return
        keep = records[-self.max_records :]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in keep), encoding="utf-8")
        tmp.replace(self.path)

    def tail(self, limit: int = 100) -> list[dict]:
        """Finished leases, newest first."""
        return list(reversed(self._read_all()[-int(limit) :]))
