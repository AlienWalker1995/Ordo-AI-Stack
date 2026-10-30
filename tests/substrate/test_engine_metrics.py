"""Per-engine model performance: every engine read its own way and reported with one definition.

Before this, Grafana showed the GPU engine through the model gateway's counters (which count
prefix-cache hits as prompt work: 4,614 tok/s against the engine's own 2,810 on 2026-10-30) and
the CPU engine through llama.cpp's, side by side as if comparable. These pin the readers against
real engine output and the `ordo_engine_*` family they feed.
"""
from __future__ import annotations

import pytest

from ordo.control import metrics
from ordo.control.engine_metrics import (
    EngineCollector,
    EngineSample,
    NinferLogReader,
    parse_duration,
    parse_llamacpp,
)

# A llama.cpp /metrics body as the CPU engine served it (2026-10-30), trimmed.
LLAMACPP_BODY = """\
# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed, excluding cached tokens
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 215518
llamacpp:prompt_tokens_cached_total 768746
llamacpp:prompt_seconds_total 4382.99
llamacpp:tokens_predicted_total 8071
llamacpp:tokens_predicted_seconds_total 597.055
llamacpp:n_decode_total 3020
llamacpp:requests_processing 1
llamacpp:requests_deferred 9
llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 2701
"""

# NInfer log lines as the GPU engine wrote them (2026-10-30).
DONE = ("2026-10-30 09:34:28.903  INFO  req#54 done | openai-chat | output limit | prompt 1,722 | output 3,000 | "
        "cache 0 (0.0%) | TTFT 39.5s | total 59.7s | queue 39.1s | prefill 7.35k tok/s | decode 148.4 tok/s")
DONE_CACHED = ("2026-10-30 09:35:49.815  INFO  req#62 done | openai-chat | tool calls 1 | prompt 90,664 | output 541 | "
               "cache 90,185 (99.5%, shared prefix) | TTFT 1m 17.4s | total 1m 20.7s | queue 1m 17.1s | prefill 1.95k tok/s")
CANCELLED = ("2026-10-30 09:35:46.271  INFO  req#66 done | openai-chat | cancelled | prompt 60 | output 0 | "
             "cache 0 (0.0%) | TTFT 665 us | total 1m 0.0s | queue 1m 0.0s | thinking 0/32,768")
NO_QUEUE = ("2026-10-30 11:00:10.247  INFO  req#11 done | openai-chat | tool calls 2 | prompt 22,997 | output 299 | "
            "cache 16,921 (73.6%, private endpoint) | TTFT 897 ms | total 2.5s | prefill 7.04k tok/s")
THROUGHPUT = ("2026-10-30 09:34:30.717  INFO  throughput | 5.0s | prefill 2.87k tok/s (14,336 tok) | "
              "decode 92.6 tok/s (463 tok) | running 1 (prefill 1) | waiting 7 | batch 1.00 | host 5.9% (295 ms)")


# --- readers ------------------------------------------------------------------------------------

def test_llamacpp_metrics_map_onto_the_engine_family():
    counters, gauges = parse_llamacpp(LLAMACPP_BODY)
    assert counters == {"prompt_tokens_processed": 215518, "prompt_tokens_cached": 768746,
                        "prompt_seconds": 4382.99, "output_tokens": 8071, "output_seconds": 597.055}
    assert gauges == {"requests_running": 1, "requests_waiting": 9}


def test_a_body_without_llamacpp_series_is_an_error_not_zeros():
    with pytest.raises(ValueError, match="--metrics"):
        parse_llamacpp("# nothing here\nup 1\n")


@pytest.mark.parametrize("text,seconds", [
    ("39.5s", 39.5), ("1m 4.1s", 64.1), ("665 us", 665e-6), ("313 ms", 0.313), ("1h 2m", 3720.0), ("1m 0.0s", 60.0),
])
def test_ninfer_durations(text, seconds):
    assert parse_duration(text) == pytest.approx(seconds)


def test_ninfer_requests_count_computed_work_not_cache_hits():
    reader = NinferLogReader(started_at=0)
    reader.consume([("t1", DONE), ("t2", DONE_CACHED), ("t3", CANCELLED)])
    c = reader.counters
    assert c["prompt_tokens_processed"] == 1722 + (90664 - 90185) + 60
    assert c["prompt_tokens_cached"] == 90185
    assert c["output_tokens"] == 3541
    # prefill = TTFT - queue; generation = total - TTFT; a request cancelled while queued adds no prefill.
    assert c["prompt_seconds"] == pytest.approx((39.5 - 39.1) + (77.4 - 77.1))
    assert c["output_seconds"] == pytest.approx((59.7 - 39.5) + (80.7 - 77.4) + (60.0 - 665e-6))
    assert c["queue_seconds"] == pytest.approx(39.1 + 77.1 + 60.0)


def test_a_request_that_did_not_wait_has_no_queue_field():
    reader = NinferLogReader(started_at=0)
    reader.consume([("t1", NO_QUEUE)])
    assert reader.counters["prompt_tokens_processed"] == 22997 - 16921
    assert reader.counters["prompt_seconds"] == pytest.approx(0.897)
    assert reader.counters["queue_seconds"] == 0


def test_ninfer_queue_gauges_follow_the_throughput_line_and_reset_when_idle():
    reader = NinferLogReader(started_at=0)
    reader.consume([("t1", THROUGHPUT)])
    assert reader.gauges == {"requests_running": 1, "requests_waiting": 7}
    reader.consume([])
    assert reader.gauges == {"requests_running": 0, "requests_waiting": 0}


def test_ninfer_lines_at_the_resume_instant_are_not_counted_twice():
    """`docker logs --since` is inclusive: the next read returns the last read's final lines again."""
    reader = NinferLogReader(started_at=0)
    reader.consume([("t1", DONE)])
    assert reader.since == "t1"
    reader.consume([("t1", DONE), ("t2", DONE_CACHED)])
    assert reader.counters["output_tokens"] == 3541
    assert reader.since == "t2"


def test_an_unreadable_request_line_is_skipped_not_fatal():
    reader = NinferLogReader(started_at=0)
    reader.consume([("t1", "req#9 done | openai-chat | prompt lots")])
    assert reader.counters["output_tokens"] == 0


# --- the collector ------------------------------------------------------------------------------

def _record(service, model, fmt, device="gpu", port=8080):
    return {"id": model, "service": service, "kind": "chat", "device": device,
            "metrics": {"format": fmt, "path": "/metrics", "port": port}}


ENGINES = {
    "local-chat": _record("inference-engine-gpu", "local-chat", "ninfer-log"),
    "local-chat-cpu": _record("inference-engine-cpu", "local-chat-cpu", "llamacpp", device="cpu"),
    "local-embed": _record("inference-engine-embed", "local-embed", "none", device="cpu"),
}


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _collector(http=None, logs=None, engines=None, clock=None):
    calls = {"engines": 0, "since": []}

    def list_engines():
        calls["engines"] += 1
        return engines or ENGINES

    def read_logs(service, since, tail):
        calls["since"].append(since)
        return logs(service) if logs else []

    collector = EngineCollector(list_engines, http or (lambda url: LLAMACPP_BODY), read_logs,
                                clock=clock or _Clock())
    return collector, calls


def test_every_measured_engine_is_read_its_own_way():
    collector, calls = _collector(logs=lambda service: [("t1", DONE)])
    samples = {s.labels["engine"]: s for s in collector.collect({"inference-engine-gpu", "inference-engine-cpu"})}
    assert set(samples) == {"inference-engine-gpu", "inference-engine-cpu"}     # embed has no metrics
    assert samples["inference-engine-cpu"].counters["output_tokens"] == 8071
    assert samples["inference-engine-cpu"].labels == {"engine": "inference-engine-cpu", "model": "local-chat-cpu",
                                                      "device": "cpu", "kind": "chat"}
    assert samples["inference-engine-gpu"].counters["output_tokens"] == 3000
    assert calls["since"] == ["1000"]   # counting starts when ops-controller does


def test_an_unreachable_engine_is_down_with_no_counters():
    def refuse(url):
        raise OSError("connection refused")
    collector, _ = _collector(http=refuse)
    cpu = next(s for s in collector.collect(None) if s.labels["engine"] == "inference-engine-cpu")
    assert not cpu.up and cpu.counters == {}


def test_a_stopped_log_read_engine_is_down():
    """A stopped container's log is still readable, so its state decides, not the log read."""
    collector, calls = _collector(logs=lambda service: [("t1", DONE)])
    gpu = next(s for s in collector.collect({"inference-engine-cpu"}) if s.labels["engine"] == "inference-engine-gpu")
    assert not gpu.up and calls["since"] == []


def test_the_engine_list_is_re_read_at_most_every_minute():
    clock = _Clock()
    collector, calls = _collector(clock=clock)
    collector.collect(None)
    clock.now += 30
    collector.collect(None)
    assert calls["engines"] == 1
    clock.now += 31
    collector.collect(None)
    assert calls["engines"] == 2


def test_a_plugin_engine_is_measured_with_no_code_change():
    engines = {"vendor-chat": _record("vendor-engine", "vendor-chat", "llamacpp", port=9000)}
    urls = []
    collector, _ = _collector(http=lambda url: urls.append(url) or LLAMACPP_BODY, engines=engines)
    assert collector.collect(None)[0].up
    assert urls == ["http://vendor-engine:9000/metrics"]


# --- the exported family ------------------------------------------------------------------------

def test_the_metrics_text_carries_one_family_for_every_engine():
    gpu = EngineSample({"engine": "inference-engine-gpu", "model": "local-chat", "device": "gpu", "kind": "chat"},
                       up=True, counters={"output_tokens": 3000, "queue_seconds": 39.1},
                       gauges={"requests_waiting": 7})
    cpu = EngineSample({"engine": "inference-engine-cpu", "model": "local-chat-cpu", "device": "cpu", "kind": "chat"},
                       up=False)
    text = metrics.render(metrics.Inputs(engines=[gpu, cpu]))
    assert ('ordo_engine_output_tokens_total{engine="inference-engine-gpu",model="local-chat",device="gpu",'
            'kind="chat"} 3000') in text
    assert 'ordo_engine_requests_waiting{engine="inference-engine-gpu"' in text
    assert 'ordo_engine_up{engine="inference-engine-cpu",model="local-chat-cpu",device="cpu",kind="chat"} 0' in text
    assert 'engine="inference-engine-cpu"' not in text.split("ordo_engine_up")[-1].split("\n", 1)[1]
    assert 'ordo_metrics_collector_ok{collector="engines"} 1' in text


def test_an_unwired_engine_collector_is_not_reported():
    """A control plane without Docker has no engine collector; that is not a failed one."""
    text = metrics.render(metrics.Inputs())
    assert "ordo_engine_" not in text and 'collector="engines"' not in text


def test_an_unreadable_engine_list_is_a_failed_collector():
    text = metrics.render(metrics.Inputs(engines_failed=True))
    assert 'ordo_metrics_collector_ok{collector="engines"} 0' in text
