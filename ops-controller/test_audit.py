import json
import threading
from pathlib import Path

from ops_controller.audit import AuditLog


def test_writes_one_line_per_call(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(action="container.restart", target="foo", result="ok", caller="test")
    log.record(action="compose.up", target="all", result="ok", caller="test")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["action"] == "container.restart"
    assert parsed[1]["action"] == "compose.up"
    assert "ts" in parsed[0]


def test_rotates_at_size_cap(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=200)
    for i in range(20):
        log.record(action="container.restart", target=f"c{i}", result="ok", caller="t")
    assert (tmp_path / "audit.1.jsonl").exists()
    assert (tmp_path / "audit.jsonl").stat().st_size < 1024


def test_record_returns_the_logged_dict(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    rec = log.record(action="container.logs", target="foo", result="ok", caller="t")
    assert rec["action"] == "container.logs"
    assert rec["caller"] == "t"


def test_concurrent_writes_dont_interleave(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    def worker(i):
        for j in range(50):
            log.record(action="x", target=f"{i}-{j}", result="ok", caller="t")
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 8 * 50
    for line in lines:
        json.loads(line)  # never raises
