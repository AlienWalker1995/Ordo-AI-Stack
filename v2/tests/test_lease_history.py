"""LeaseHistory sink: lifecycle, outcomes, torn lines, trim, tail order."""
import json

from ordo.lease_history import LeaseHistory


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _hist(tmp_path, **kw):
    clock = _Clock()
    return LeaseHistory(tmp_path / "hist.jsonl", now_fn=clock, **kw), clock


def test_full_lifecycle_records_completed(tmp_path):
    h, clock = _hist(tmp_path)
    h.submitted("train", "training", 30)
    clock.t = 1010.0
    h.started("train")
    clock.t = 1300.0
    h.ended("train", "completed")
    (rec,) = h.tail()
    assert rec["id"] == "train" and rec["kind"] == "training" and rec["vram_gb"] == 30.0
    assert rec["submitted"] == 1000.0 and rec["started"] == 1010.0 and rec["ended"] == 1300.0
    assert rec["outcome"] == "completed"


def test_rejected_and_swept_outcomes(tmp_path):
    h, _ = _hist(tmp_path)
    h.submitted("huge", "media", 99)
    h.rejected("huge")
    h.submitted("crashy", "training", 30)
    h.started("crashy")
    h.ended("crashy", "swept")
    ids = {r["id"]: r["outcome"] for r in h.tail()}
    assert ids == {"huge": "rejected", "crashy": "swept"}
    assert h.tail()[0]["id"] == "crashy"  # newest first


def test_unknown_end_is_noop_and_double_end_writes_once(tmp_path):
    h, _ = _hist(tmp_path)
    h.ended("ghost", "completed")            # never submitted — controller restart case
    h.submitted("a", "chat", 4)
    h.ended("a", "completed")
    h.ended("a", "completed")                # second end: pending already popped
    assert [r["id"] for r in h.tail()] == ["a"]


def test_torn_line_does_not_poison_history(tmp_path):
    h, _ = _hist(tmp_path)
    h.submitted("a", "chat", 4)
    h.ended("a", "completed")
    with h.path.open("a", encoding="utf-8") as f:
        f.write('{"id": "torn', )  # crash mid-write
    h.submitted("b", "media", 17)
    h.ended("b", "completed")
    assert [r["id"] for r in h.tail()] == ["b", "a"]


def test_trim_keeps_newest(tmp_path):
    h, _ = _hist(tmp_path, max_records=5, trim_threshold=10)
    for i in range(12):
        h.submitted(f"j{i}", "chat", 1)
        h.ended(f"j{i}", "completed")
    records = [json.loads(x) for x in h.path.read_text().splitlines()]
    assert len(records) <= 10
    assert h.tail(3) == h.tail()[:3]
    assert h.tail()[0]["id"] == "j11"  # newest survives the trim
