"""Prometheus and Grafana measure every model engine the same way, and name none of them.

Model performance comes from ops-controller's `ordo_engine_*` family (ordo/control/engine_metrics.py),
which reads each engine its own way (llama.cpp's /metrics, NInfer's request log) and labels it by
engine, model and device. The dashboard picks engines with an `$engine` variable, so a host with no
GPU, two GPUs or a plugin's engine needs no edit here. Before this, the GPU panels read the model
gateway (which counts prefix-cache hits as prompt work, 64% high on 2026-10-30) and the CPU panels
read llama.cpp, side by side as if comparable.
"""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROM = yaml.safe_load((ROOT / "monitoring" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
DASH = json.loads((ROOT / "monitoring" / "grafana" / "dashboards" / "ordo-llm-gpu.json").read_text(encoding="utf-8"))


def _jobs():
    return {j["job_name"]: j for j in PROM["scrape_configs"]}


def _panels():
    return [p for p in DASH["panels"] if p["type"] != "row"]


def _exprs():
    return [t.get("expr", "") for p in _panels() for t in p.get("targets", [])]


def _engine_exprs():
    return [e for e in _exprs() if "ordo_engine_" in e]


def test_container_series_keep_their_own_service_label():
    """Without honor_labels the target's `service: ops-controller` replaced every container's name, so
    each container alert named ops-controller instead of the service that failed (2026-10-30)."""
    assert _jobs()["ops-controller"].get("honor_labels") is True


def test_no_engine_is_scraped_by_name():
    """Engines are read by ops-controller from the registry; a per-engine job is a list to keep in sync
    (and NInfer's target was down by design, since it has no /metrics)."""
    targets = [t for j in PROM["scrape_configs"] for c in j.get("static_configs", []) for t in c["targets"]]
    assert not [t for t in targets if t.startswith("inference-engine")]


def test_no_panel_names_an_engine():
    leaks = [e for e in _exprs() if "inference-engine" in e or "api_base=" in e or "llamacpp:" in e]
    assert not leaks, f"these panels hardcode an engine: {leaks}"


def test_every_engine_panel_follows_the_engine_variable():
    names = [v["name"] for v in DASH["templating"]["list"]]
    assert "engine" in names
    variable = next(v for v in DASH["templating"]["list"] if v["name"] == "engine")
    assert "label_values(ordo_engine_up, engine)" in json.dumps(variable)
    assert _engine_exprs()
    assert all('engine=~"$engine"' in e for e in _engine_exprs())


def test_prompt_work_excludes_cache_hits():
    computed = [e for e in _engine_exprs() if "prompt_tokens_" in e]
    assert computed and all("prompt_tokens_processed" in e for e in computed)


def test_speeds_are_measured_while_working_not_per_request():
    """A queued request is not a slow model: speeds divide tokens by the time spent on them."""
    assert any("output_tokens" in e and "output_seconds" in e for e in _engine_exprs())
    assert any("prompt_tokens_processed" in e and "prompt_seconds" in e for e in _engine_exprs())
    assert not [e for e in _exprs() if "litellm_llm_api_latency" in e or "latency_per_output_token" in e]


def test_the_dashboard_shows_why_requests_moved_to_a_fallback():
    assert any("litellm_deployment_successful_fallbacks_total" in e and "exception_class" in e for e in _exprs())


def test_panels_do_not_overlap():
    boxes = [(p["title"], p["gridPos"]) for p in DASH["panels"]]
    for i, (a_title, a) in enumerate(boxes):
        for b_title, b in boxes[i + 1:]:
            overlap = (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                       and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])
            assert not overlap, f"{a_title!r} overlaps {b_title!r}"


def test_the_embed_uid_is_stable():
    """The Ordo dashboard embeds /grafana/d/ordo-llm-gpu; renaming the uid breaks its Performance page."""
    assert DASH["uid"] == "ordo-llm-gpu"
