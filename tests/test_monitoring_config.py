"""Prometheus and Grafana must show both chat engines, separately.

The CPU fallback answers chat whenever a render borrows the GPU, so a performance view that only
scrapes the GPU server is blind exactly when it matters. Both servers expose the same llama.cpp
metric names, so once both are scraped every query must say which one it means: an unfiltered
`llamacpp:*` query would silently merge them into one unlabeled series.
"""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROM = yaml.safe_load((ROOT / "monitoring" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
DASH = json.loads((ROOT / "monitoring" / "grafana" / "dashboards" / "ordo-llm-gpu.json").read_text(encoding="utf-8"))


def _jobs():
    return {j["job_name"]: j for j in PROM["scrape_configs"]}


def _exprs():
    return [t.get("expr", "") for p in DASH["panels"] for t in p.get("targets", [])]


def test_the_cpu_fallback_is_scraped():
    job = _jobs()["llamacpp-cpu"]
    assert job["static_configs"][0]["targets"] == ["llamacpp-cpu:8080"]


def test_every_llama_cpp_query_names_its_server():
    unfiltered = [e for e in _exprs() if "llamacpp:" in e and not re.search(r'job=~?"', e)]
    assert not unfiltered, f"these would merge the GPU and CPU servers: {unfiltered}"


def test_the_dashboard_shows_the_cpu_fallback():
    assert any('job="llamacpp-cpu"' in e or "llamacpp-cpu" in e for e in _exprs())


def test_the_dashboard_shows_when_each_chat_server_was_up():
    """A render evicts the GPU server; `up` over time is what makes that visible."""
    assert any(e.startswith("up{") and "llamacpp" in e for e in _exprs())


def test_panels_do_not_overlap():
    boxes = [(p["title"], p["gridPos"]) for p in DASH["panels"]]
    for i, (a_title, a) in enumerate(boxes):
        for b_title, b in boxes[i + 1:]:
            overlap = (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                       and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])
            assert not overlap, f"{a_title!r} overlaps {b_title!r}"


def test_the_embed_uid_is_stable():
    """The dashboard's Performance page embeds this uid."""
    assert DASH["uid"] == "ordo-llm-gpu"
