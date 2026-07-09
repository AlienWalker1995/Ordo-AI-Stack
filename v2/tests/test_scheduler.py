"""Scheduler decision-engine behavior."""
from ordo.scheduler import Job, Scheduler


def test_chat_co_runs_beside_media_when_it_fits():
    s = Scheduler(total_vram_gb=32)
    s.submit(Job("media", 20, "media"))
    s.submit(Job("chat", 4, "chat"))
    admitted, _ = s.pump()
    # both run concurrently — chat slips in beside media (no starvation cliff)
    assert set(admitted) == {"media", "chat"}
    assert set(s.running_ids) == {"media", "chat"}
    assert s.free_vram_gb == 32 - 24


def test_non_fitting_head_waits_then_admits_on_completion():
    s = Scheduler(total_vram_gb=32)
    s.submit(Job("m1", 20, "media"))
    s.submit(Job("m2", 20, "media"))
    admitted, _ = s.pump()
    assert admitted == ["m1"]           # m2 doesn't fit (20 > 12 free)
    assert s.queued_ids == ["m2"]
    s.complete("m1")
    admitted, _ = s.pump()
    assert admitted == ["m2"]           # frees up → m2 admitted (FIFO order preserved)


def test_per_item_batch_lets_chat_interleave():
    s = Scheduler(total_vram_gb=32)
    s.submit(Job("chat", 4, "chat"))
    for i in range(13):
        s.submit(Job(f"song{i}", 10, "batch_item"))
    admitted, _ = s.pump()
    # chat ran immediately; a couple song items co-run; the rest queue (per-item, not one block)
    assert "chat" in admitted
    assert s.running_ids.count("chat") == 0 or "chat" in s.running_ids
    assert "chat" in s.running_ids
    assert len(s.running_ids) >= 2       # chat + at least one song concurrently
    # drain a couple items and confirm more get admitted (no monolithic block)
    first_song = next(j for j in s.running_ids if j.startswith("song"))
    s.complete(first_song)
    more, _ = s.pump()
    assert any(x.startswith("song") for x in more)


def test_fifo_order_preserved():
    s = Scheduler(total_vram_gb=100)
    for n in ["a", "b", "c"]:
        s.submit(Job(n, 10))
    admitted, _ = s.pump()
    assert admitted == ["a", "b", "c"]


def test_lru_evicts_idle_cached_to_admit():
    s = Scheduler(total_vram_gb=32)
    s.cache_idle("old_model", 20)        # an idle cached model holding VRAM (oldest)
    s.cache_idle("newer_model", 6)
    s.submit(Job("chat", 12, "chat"))    # needs 12; free = 32-26 = 6 → must evict
    admitted, evicted = s.pump()
    assert admitted == ["chat"]
    assert "old_model" in evicted        # LRU (oldest) evicted first
    assert "newer_model" not in evicted  # only evicted as much as needed


def test_job_bigger_than_gpu_stays_queued():
    s = Scheduler(total_vram_gb=8)
    s.submit(Job("huge", 20, "media"))   # never fits → would cloud-fallback in the real broker
    admitted, _ = s.pump()
    assert admitted == []
    assert s.queued_ids == ["huge"]
