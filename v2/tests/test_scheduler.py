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


def test_job_bigger_than_gpu_is_removed_not_left_to_starve_the_queue():
    # A job that can never fit is rejected (or cloud-routed) rather than blocking the queue head.
    # See test_cloud_fallback.py for the full routing/starvation coverage.
    s = Scheduler(total_vram_gb=8)
    s.submit(Job("huge", 20, "media"))   # never fits on an 8GB card
    admitted, _ = s.pump()
    assert admitted == []
    assert s.queued_ids == []                    # not left stuck in the queue
    assert s.status()["rejected"] == ["huge"]    # rejected (no cloud fallback configured here)


# ── media-lease semantics: evict the resident LLM for a media job, RESTORE it on completion ──────

def _sched_with_resident(total=32, resident_gb=25):
    """A scheduler with the resident LLM registered as idle-cached (the serve-startup wiring)."""
    s = Scheduler(total_vram_gb=total)
    s.cache_idle("llamacpp", resident_gb)   # ~weights+KV footprint — leaves too little for a media job
    return s


def test_media_job_evicts_resident_and_records_it_for_restore():
    s = _sched_with_resident()               # 32 total, 25 resident -> 7 free (a media job won't fit)
    s.submit(Job("reel", 18, "media"))
    admitted, evicted = s.pump()
    assert admitted == ["reel"]
    assert evicted == ["llamacpp"]                       # resident stopped to free VRAM
    assert "llamacpp" in s.evicted_residents             # tracked with its footprint for restore
    assert s.evicted_residents["llamacpp"] == 25
    assert "llamacpp" not in s.idle_cached               # no longer cached (it's stopped)


def test_resident_restored_when_media_completes_and_queue_drains():
    s = _sched_with_resident()
    s.submit(Job("reel", 18, "media"))
    s.pump()                                             # evicts llamacpp, runs reel
    s.complete("reel")                                   # media done -> queue empty
    restored = s.take_restorable()
    assert restored == {"llamacpp": 25}                  # resident restarted with its footprint
    assert "llamacpp" in s.idle_cached                   # re-armed as evictable for the next lease
    assert s.evicted_residents == {}


def test_no_restore_while_a_media_job_still_running():
    s = _sched_with_resident()
    s.submit(Job("reel", 18, "media"))
    s.pump()
    # reel still running — restoring the LLM now would immediately need re-eviction: don't.
    assert s.take_restorable() == {}
    assert "llamacpp" in s.evicted_residents


def test_no_thrash_between_back_to_back_media_jobs():
    # Two queued media renders: complete the first, and the resident must NOT flap on/off — it stays
    # evicted until the WHOLE media queue drains (the anti-thrash rule).
    s = _sched_with_resident()
    s.submit(Job("reel1", 18, "media"))
    s.submit(Job("reel2", 18, "media"))
    s.pump()                                             # reel1 runs (evicts llamacpp); reel2 queued
    assert s.running_ids == ["reel1"] and s.queued_ids == ["reel2"]
    s.complete("reel1")                                  # reel2 now admittable
    assert s.take_restorable() == {}                     # DON'T restore — reel2 still needs the card
    s.pump()                                             # reel2 admitted
    assert s.running_ids == ["reel2"]
    assert s.take_restorable() == {}                     # still no restore while reel2 runs
    s.complete("reel2")
    assert s.take_restorable() == {"llamacpp": 25}       # only now — queue fully drained


def test_lease_ttl_auto_completes_stranded_job():
    # A crashed client never calls complete() — the TTL sweep force-completes the lease so the
    # resident can be restored (self-heal; a stranded lease must never kill the brain forever).
    s = _sched_with_resident()
    s.submit(Job("reel", 18, "media", est_seconds=60))   # TTL = 60 * 2 = 120s
    s.pump()
    s.tick(90)
    assert s.sweep_expired_leases() == []                # not yet past the 120s TTL
    assert s.running_ids == ["reel"]
    s.tick(40)                                           # now 130s elapsed > 120s TTL
    assert s.sweep_expired_leases() == ["reel"]          # force-completed
    assert s.running_ids == []
    assert s.take_restorable() == {"llamacpp": 25}       # resident restored after the stranded lease


def test_lease_ttl_default_and_max_caps():
    s = Scheduler(total_vram_gb=32, lease_ttl_default=100.0, lease_ttl_max=200.0)
    # no estimate -> the default cap
    s.submit(Job("no_est", 4))
    s.pump()
    st = s.status()["running"][0]
    assert st["lease_ttl_s"] == 100.0
    # a huge estimate is clamped to the max
    s2 = Scheduler(total_vram_gb=32, lease_ttl_default=100.0, lease_ttl_max=200.0)
    s2.submit(Job("huge_est", 4, est_seconds=1000))      # 1000*2 = 2000 -> clamped to 200
    s2.pump()
    assert s2.status()["running"][0]["lease_ttl_s"] == 200.0


def test_recache_resident_clears_evicted_and_rearms():
    # take_restorable re-caches; an explicit cache_idle (e.g. after a manual restart) must also clear
    # the evicted record so the resident is a normal evictable again.
    s = _sched_with_resident()
    s.submit(Job("reel", 18, "media"))
    s.pump()
    assert "llamacpp" in s.evicted_residents
    s.cache_idle("llamacpp", 25)                         # re-registered as resident
    assert "llamacpp" not in s.evicted_residents
    assert s.idle_cached["llamacpp"] == 25


def test_status_surfaces_lease_and_resident_fields():
    s = _sched_with_resident()
    s.submit(Job("reel", 18, "media", est_seconds=120))
    s.pump()
    st = s.status()
    assert st["evicted_residents"] == {"llamacpp": 25}
    assert st["idle_cached"] == {}
    assert st["running"][0]["lease_ttl_s"] == 240.0      # 120 * 2
    assert st["free_vram_gb"] == 32 - 18                 # evicted resident holds no VRAM


# ── renewable leases: a live client heartbeats past any TTL; a dead one is still swept ────────────

def test_heartbeat_extends_lease_beyond_absolute_cap():
    s = Scheduler(total_vram_gb=32)
    s.submit(Job("train", 30, "training"))     # no est_seconds → default 1800s TTL
    s.pump()
    # simulate 2h of runtime with a beat every 60s — the lease never expires
    for _ in range(120):
        s.tick(60)
        assert s.heartbeat("train") is True
        assert s.sweep_expired_leases() == []
    assert "train" in s.running_ids


def test_dead_client_stops_beating_and_is_swept_within_heartbeat_ttl():
    s = Scheduler(total_vram_gb=32)
    s.submit(Job("train", 30, "training"))
    s.pump()
    s.tick(60)
    assert s.heartbeat("train") is True        # last sign of life
    s.tick(900)                                # HEARTBEAT_TTL elapses with no further beats
    assert s.sweep_expired_leases() == ["train"]
    assert s.running_ids == []


def test_heartbeat_unknown_or_completed_job_is_false_and_harmless():
    s = Scheduler(total_vram_gb=32)
    assert s.heartbeat("ghost") is False
    s.submit(Job("j", 4, "chat"))
    s.pump()
    s.complete("j")
    assert s.heartbeat("j") is False


def test_heartbeat_lease_restores_resident_after_sweep():
    # end-to-end lease semantics survive: a heartbeating training job that dies is swept
    # and the evicted resident becomes restorable again (llamacpp can never be stranded down).
    s = Scheduler(total_vram_gb=32)
    s.cache_idle("llamacpp", 25)
    s.submit(Job("train", 30, "training"))
    s.pump()
    assert "llamacpp" in s.evicted_residents
    s.heartbeat("train")
    s.tick(901)                                # client died right after its beat
    assert s.sweep_expired_leases() == ["train"]
    assert "llamacpp" in s.take_restorable()
