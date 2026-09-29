"""`GET /doctor`: the drift `ordo doctor` reports, as the control plane sees it (hostile audit OPS-3).

`ordo doctor` runs on the host, so an operator who only watches the dashboard never saw its
findings. ops-controller now answers the same two checks read-only: its own substrate digest
against the one the last render recorded in out/manifest.json, and the open-webui probe. Both are
judged by the functions `ordo doctor` uses (`substrate.substrate_verdict`, `open_webui_verdict`),
never a copy of them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ordo import cli
from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Scheduler
from ordo.host import doctor
from ordo.render import substrate
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
SOURCE = {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto", "plugins": "auto"}
HEALTHY_PROBE = {"persistent_config": "false", "default_model": "local-chat", "embed_model": "local-embed",
                 "chat": {"status": 200, "models": ["local-chat", "local-embed"]},
                 "rag": {"status": 200, "models": ["local-chat", "local-embed"]}}


def _plane(tmp_path, recorded: str | None = "unset", services=("open-webui",), broker=True):
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(SOURCE, sort_keys=False), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    if recorded != "unset":
        manifest = {"tier": "ultra"} if recorded is None else {"substrate_digest": recorded}
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    backend = MockBackend({"services": {name: {"image": f"example/{name}"} for name in services}})
    backend.exec_result = (0, json.dumps(HEALTHY_PROBE) + "\n")
    scheduler = Scheduler(32)
    plane = ControlPlane(src, CATALOG, REGISTRY, out, scheduler=scheduler,
                         broker=Broker(scheduler, backend) if broker else None)
    return plane, backend


def _check(body: dict, name: str) -> dict:
    return next(check for check in body["checks"] if check["check"] == name)


def test_a_clean_stack_reports_no_drift(tmp_path):
    plane, _ = _plane(tmp_path, recorded=substrate.current_digest())
    code, body = plane.route("GET", "/doctor")
    assert code == 200
    assert body["ok"] is True
    assert [check["check"] for check in body["checks"]] == ["substrate", "open-webui"]
    assert all(check["ok"] for check in body["checks"])


def test_a_substrate_mismatch_with_the_last_render_is_a_finding(tmp_path):
    plane, _ = _plane(tmp_path, recorded="0" * 64)
    code, body = plane.route("GET", "/doctor")
    assert code == 200 and body["ok"] is False
    finding = _check(body, "substrate")
    assert finding["ok"] is False
    assert "MISMATCH" in finding["detail"] and "ordo recreate ops-controller" in finding["detail"]
    assert substrate.current_digest()[:12] in finding["detail"] and "0" * 12 in finding["detail"]
    # The CLI's "! " marker is presentation; the route reports the finding without it.
    assert not finding["detail"].startswith("!")


@pytest.mark.parametrize("recorded", [None, "unset"], ids=["pre-digest manifest", "no manifest"])
def test_nothing_recorded_is_not_drift(tmp_path, recorded):
    plane, _ = _plane(tmp_path, recorded=recorded)
    code, body = plane.route("GET", "/doctor")
    assert code == 200 and _check(body, "substrate")["ok"] is True


def test_an_unreadable_manifest_is_a_finding(tmp_path):
    plane, _ = _plane(tmp_path)
    (tmp_path / "out" / "manifest.json").write_text("{not json", encoding="utf-8")
    code, body = plane.route("GET", "/doctor")
    assert code == 200
    finding = _check(body, "substrate")
    assert finding["ok"] is False and "manifest.json" in finding["detail"]


def test_open_webui_is_probed_in_its_running_container(tmp_path):
    plane, backend = _plane(tmp_path, recorded=substrate.current_digest())
    backend.exec_result = (0, json.dumps({**HEALTHY_PROBE, "chat": {"status": 401, "models": []}}) + "\n")
    code, body = plane.route("GET", "/doctor")
    assert code == 200 and body["ok"] is False
    finding = _check(body, "open-webui")
    assert finding["ok"] is False and "401" in finding["detail"]
    assert [container for container, _ in backend.execs] == ["ordo-open-webui-1"]


@pytest.mark.parametrize("services", [(), ("open-webui",)], ids=["not rendered", "stopped"])
def test_an_open_webui_that_is_not_running_is_not_probed(tmp_path, services):
    plane, backend = _plane(tmp_path, recorded=substrate.current_digest(), services=services)
    if services:
        backend.stop("open-webui")
    code, body = plane.route("GET", "/doctor")
    assert code == 200
    assert _check(body, "open-webui") == {"check": "open-webui", "ok": True, "detail": "open-webui: not running"}
    assert backend.execs == []


def test_a_probe_that_fails_is_a_finding(tmp_path):
    plane, backend = _plane(tmp_path, recorded=substrate.current_digest())
    backend.exec_result = (1, "Traceback ...\nModuleNotFoundError: no json\n")
    code, body = plane.route("GET", "/doctor")
    finding = _check(body, "open-webui")
    assert code == 200 and finding["ok"] is False and "ModuleNotFoundError" in finding["detail"]


def test_without_a_container_backend_open_webui_cannot_be_checked(tmp_path):
    plane, _ = _plane(tmp_path, recorded=substrate.current_digest(), broker=False)
    code, body = plane.route("GET", "/doctor")
    finding = _check(body, "open-webui")
    assert code == 200 and finding["ok"] is False and "no container backend" in finding["detail"]


def test_doctor_is_read_only(tmp_path):
    plane, backend = _plane(tmp_path, recorded="0" * 64)
    before = sorted(p.name for p in (tmp_path / "out").iterdir())
    plane.route("GET", "/doctor")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == before
    assert backend.recreate_calls == [] and backend.stopped == [] and backend.restarted == []


def test_the_cli_and_the_route_judge_the_substrate_with_one_function(tmp_path, monkeypatch, capsys):
    """Not a copy: both callers go through `substrate.substrate_verdict`."""
    calls = []

    def verdict(ops_controller, reference, *, reference_name, rebuild_from):
        calls.append((ops_controller, reference))
        return False, "! substrate: judged once"

    monkeypatch.setattr(substrate, "substrate_verdict", verdict)
    plane, _ = _plane(tmp_path, recorded="1" * 64)
    _code, body = plane.route("GET", "/doctor")
    assert _check(body, "substrate")["detail"] == "substrate: judged once"

    monkeypatch.setattr(doctor, "read_running_substrate_digest", lambda project: "2" * 64)
    monkeypatch.setattr(doctor, "read_open_webui_probe", lambda project: None)
    assert cli.main(["doctor"]) == 1
    assert "! substrate: judged once" in capsys.readouterr().out
    assert calls == [(substrate.current_digest(), "1" * 64), ("2" * 64, substrate.current_digest())]
