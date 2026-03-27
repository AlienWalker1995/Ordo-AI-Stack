"""Orchestration HTTP layer: validation, readiness, templates (no live ComfyUI)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.workflow_boundary import assert_api_workflow, is_ui_workflow_export


@pytest.fixture
def client():
    return TestClient(app)


def test_ui_export_detection():
    ui = {"nodes": [{"type": "Foo", "id": 1}]}
    assert is_ui_workflow_export(ui) is True
    api = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}}}
    assert is_ui_workflow_export(api) is False


def test_assert_api_workflow_rejects_ui():
    ui = {"nodes": [{"type": "Foo", "id": 1}]}
    with pytest.raises(ValueError, match="UI"):
        assert_api_workflow(ui)


def test_readiness_ok_when_probes_pass():
    from dashboard.orchestration_readiness import compute_readiness

    with patch(
        "dashboard.orchestration_readiness._probe_get",
        return_value=(True, None),
    ):
        r = compute_readiness()
    assert r["ok"] is True


def test_validate_endpoint_rejects_ui(client: TestClient):
    ui = {"nodes": [{"type": "Foo", "id": 1}]}
    r = client.post("/api/orchestration/validate", json={"workflow": ui})
    assert r.status_code == 400


def test_readiness_endpoint_503_when_not_ready(client: TestClient):
    with patch(
        "dashboard.routes_orchestration.compute_readiness",
        return_value={"ok": False, "checks": []},
    ):
        r = client.get("/api/orchestration/readiness")
    assert r.status_code == 503


def test_readiness_public_no_auth(client: TestClient):
    with patch(
        "dashboard.routes_orchestration.compute_readiness",
        return_value={"ok": True, "checks": []},
    ):
        r = client.get("/api/orchestration/readiness")
    assert r.status_code == 200


def test_template_compile_minimal(tmp_path: Path, monkeypatch):
    from dashboard.workflow_templates import compile_template

    wf_dir = tmp_path / "wf"
    wf_dir.mkdir()
    wf_file = wf_dir / "generate_image.json"
    wf_file.write_text(
        json.dumps(
            {
                "9": {
                    "class_type": "CLIPTextEncode",
                    "inputs": {"text": "PARAM_STR_prompt", "clip": ["1", 1]},
                }
            }
        ),
        encoding="utf-8",
    )
    tpl = {
        "id": "generate_image",
        "workflow_file": "generate_image.json",
        "parameter_schema": {
            "type": "object",
            "required": ["prompt"],
            "properties": {"prompt": {"type": "string", "minLength": 1}},
        },
    }
    out = compile_template(tpl, {"prompt": "hello"}, workflows_dir=wf_dir)
    assert out["9"]["inputs"]["text"] == "hello"
