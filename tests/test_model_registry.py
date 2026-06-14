from __future__ import annotations
import json
from pathlib import Path
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "model_registry",
    Path(__file__).resolve().parent.parent / "ops-controller" / "model_registry.py",
)
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)


def _reg(tmp_path) -> "mr.ModelRegistry":
    return mr.ModelRegistry(
        registry_path=tmp_path / "model-registry.json",
        env_path=tmp_path / ".env",
        gpu_assignments_path=tmp_path / "gpu-assignments.yml",
    )


def test_empty_registry_lists_nothing(tmp_path):
    reg = _reg(tmp_path)
    assert reg.list_models() == {}


def test_upsert_then_get_roundtrips(tmp_path):
    reg = _reg(tmp_path)
    rec = mr.ModelRecord(
        id="local-chat", kind="chat", service="llamacpp", runtime="single-model",
        source={"file": "x.gguf"}, gpu_uuid=None, enabled=True,
        config={"ctx": 262144}, est_vram_gb=20.0,
    )
    reg.upsert(rec)
    got = reg.get("local-chat")
    assert got is not None
    assert got.kind == "chat"
    assert got.config["ctx"] == 262144
    on_disk = json.loads((tmp_path / "model-registry.json").read_text())
    assert on_disk["models"]["local-chat"]["service"] == "llamacpp"


def test_delete_removes(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert(mr.ModelRecord(id="m1", kind="chat", service="llamacpp",
                              runtime="single-model", source={"file": "a.gguf"},
                              enabled=False, est_vram_gb=1.0))
    reg.delete("m1")
    assert reg.get("m1") is None
