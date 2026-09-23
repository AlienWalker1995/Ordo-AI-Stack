"""SQLite-backed workflow version store (save, list, diff, promote, roll back).

The media worker's job queue, publish outbox and schedules lived here too until that worker was
retired (2026-07-31); DB files created before then still hold those tables, unused.

Journal mode is DELETE (a plain rollback journal), NOT WAL: this DB lives on a 9p/Windows
Docker bind mount, where WAL's shared-memory `-shm` mmap is unreliable ("disk I/O error") and a
stale `-shm` from an abruptly-replaced container gets stuck busy. See `_connect`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _db_path(data_dir: Path) -> Path:
    d = data_dir / "orchestration"
    d.mkdir(parents=True, exist_ok=True)
    # `.sqlite3`, not the old `.db`: a stale WAL `-shm` mmap on the 9p bind mount can get
    # wedged busy (undeletable until a Docker restart) and disk-I/O-error the old file. A
    # fresh filename side-steps the wedged orphan; combined with DELETE journal mode above,
    # no `-shm` is ever created again so this can't recur.
    return d / "orchestration.sqlite3"


def _connect(data_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(data_dir)), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Rollback journal, NOT WAL. This db lives on a 9p/Windows Docker bind mount, where WAL's
    # shared-memory (-shm) mmap is unreliable ("disk I/O error") and a stale -shm from an
    # abruptly-replaced container gets stuck busy. DELETE mode uses a plain -journal (no
    # mmap), which is 9p-safe and fine for this small store.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    compiled_json TEXT NOT NULL,
    params_schema TEXT,
    created_at TEXT NOT NULL,
    promoted_at TEXT,
    rollback_of INTEGER,
    UNIQUE(workflow_id, version)
);

CREATE INDEX IF NOT EXISTS idx_wf_versions_lookup ON workflow_versions(workflow_id, version);
CREATE INDEX IF NOT EXISTS idx_wf_versions_promoted ON workflow_versions(workflow_id, version DESC) WHERE promoted_at IS NOT NULL;
"""


def init_db(data_dir: Path) -> None:
    """Create the workflow-versions table if it is missing."""
    with _connect(data_dir) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


# ── Workflow versions ──────────────────────────────────────────────────────────

def save_workflow_version(
    data_dir: Path,
    workflow_id: str,
    compiled_json: dict[str, Any],
    params_schema: dict[str, Any] | None = None,
) -> int:
    """Save a new version; returns the version number.

    Uses an atomic INSERT…SELECT to avoid version-number collisions
    when concurrent callers save the same workflow_id.
    """
    with _connect(data_dir) as conn:
        conn.execute(
            """INSERT INTO workflow_versions
               (workflow_id, version, compiled_json, params_schema, created_at)
               VALUES (?,
                       COALESCE((SELECT MAX(version) FROM workflow_versions WHERE workflow_id=?), 0) + 1,
                       ?, ?, ?)""",
            (
                workflow_id, workflow_id,
                json.dumps(compiled_json),
                json.dumps(params_schema) if params_schema else None,
                _now_iso(),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT MAX(version) as mv FROM workflow_versions WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
    return row["mv"]


def list_workflow_versions(data_dir: Path, workflow_id: str) -> list[dict[str, Any]]:
    with _connect(data_dir) as conn:
        rows = conn.execute(
            "SELECT id, workflow_id, version, params_schema, created_at, promoted_at, rollback_of "
            "FROM workflow_versions WHERE workflow_id=? ORDER BY version DESC",
            (workflow_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_workflow_version(
    data_dir: Path, workflow_id: str, version: int
) -> dict[str, Any] | None:
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id=? AND version=?",
            (workflow_id, version),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["compiled_json"] = json.loads(d["compiled_json"])
    except (json.JSONDecodeError, TypeError):
        pass
    return d


def promote_workflow_version(data_dir: Path, workflow_id: str, version: int) -> bool:
    with _connect(data_dir) as conn:
        now = _now_iso()
        # Demote any previously promoted versions for this workflow
        conn.execute(
            "UPDATE workflow_versions SET promoted_at=NULL WHERE workflow_id=? AND promoted_at IS NOT NULL",
            (workflow_id,),
        )
        result = conn.execute(
            "UPDATE workflow_versions SET promoted_at=? WHERE workflow_id=? AND version=?",
            (now, workflow_id, version),
        )
        conn.commit()
    return result.rowcount > 0


def get_promoted_workflow(data_dir: Path, workflow_id: str) -> dict[str, Any] | None:
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id=? AND promoted_at IS NOT NULL "
            "ORDER BY version DESC LIMIT 1",
            (workflow_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["compiled_json"] = json.loads(d["compiled_json"])
    except (json.JSONDecodeError, TypeError):
        pass
    return d


def rollback_workflow(data_dir: Path, workflow_id: str, to_version: int) -> int | None:
    """Create a new version that is a copy of to_version; returns new version number."""
    src = get_workflow_version(data_dir, workflow_id, to_version)
    if not src:
        return None
    with _connect(data_dir) as conn:
        conn.execute(
            """INSERT INTO workflow_versions
               (workflow_id, version, compiled_json, params_schema, created_at, rollback_of)
               VALUES (?,
                       COALESCE((SELECT MAX(version) FROM workflow_versions WHERE workflow_id=?), 0) + 1,
                       ?, ?, ?, ?)""",
            (
                workflow_id, workflow_id,
                json.dumps(src["compiled_json"]) if isinstance(src["compiled_json"], dict)
                else src["compiled_json"],
                src.get("params_schema"),
                _now_iso(),
                src["id"],
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT MAX(version) as mv FROM workflow_versions WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
    return row["mv"]


def load_store(data_dir: Path) -> None:
    """Called by routes_orchestration on startup; ensures the DB is ready."""
    init_db(data_dir)
