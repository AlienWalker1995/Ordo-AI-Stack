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


# ---------------------------------------------------------------------------
# Task 2: derive_env + derive_gpu_assignment
# ---------------------------------------------------------------------------

def test_derive_env_for_chat(tmp_path):
    reg = _reg(tmp_path)
    rec = mr.ModelRecord(id="local-chat", kind="chat", service="llamacpp",
                         runtime="single-model", source={"file": "qwen.gguf"},
                         enabled=True, config={"ctx": 131072, "mmproj": "mm.gguf"},
                         est_vram_gb=20.0)
    env = reg.derive_env(rec)
    assert env["LLAMACPP_MODEL"] == "qwen.gguf"
    assert env["LLAMACPP_CTX_SIZE"] == "131072"
    assert env["LLAMACPP_MMPROJ"] == "mm.gguf"

def test_derive_env_for_embedding(tmp_path):
    reg = _reg(tmp_path)
    rec = mr.ModelRecord(id="local-embed", kind="embedding", service="llamacpp-embed",
                         runtime="single-model", source={"file": "nomic.gguf"},
                         enabled=True, est_vram_gb=1.5)
    assert reg.derive_env(rec) == {"LLAMACPP_EMBED_MODEL": "nomic.gguf"}

def test_derive_gpu_assignment(tmp_path):
    reg = _reg(tmp_path)
    rec = mr.ModelRecord(id="local-chat", kind="chat", service="llamacpp",
                         runtime="single-model", source={"file": "q.gguf"},
                         gpu_uuid="GPU-abc", enabled=True, est_vram_gb=20.0)
    assert reg.derive_gpu_assignment(rec) == ("llamacpp", "GPU-abc")


# ---------------------------------------------------------------------------
# Task 3: render_gpu_assignments_yaml + capacity_check
# ---------------------------------------------------------------------------

def test_render_gpu_yaml_emits_both_layers(tmp_path):
    out = mr.render_gpu_assignments_yaml({"llamacpp": "GPU-abc", "comfyui": "GPU-def"})
    assert "CUDA_VISIBLE_DEVICES=GPU-abc" in out
    assert "NVIDIA_VISIBLE_DEVICES=GPU-abc" in out
    assert "device_ids:" in out and "GPU-def" in out
    assert out.lstrip().startswith("services:")

def test_capacity_check_blocks_overcommit():
    gpus = {"GPU-1": {"total_gb": 8.0}}
    enabled = [
        mr.ModelRecord(id="a", kind="stt", service="stt", runtime="single-model",
                       source={}, gpu_uuid="GPU-1", enabled=True, est_vram_gb=5.0),
    ]
    ok, used, total = mr.capacity_check(gpus, "GPU-1", enabled, candidate_gb=4.0)
    assert used == 5.0 and total == 8.0 and ok is False

def test_capacity_check_allows_fit():
    gpus = {"GPU-1": {"total_gb": 32.0}}
    ok, used, total = mr.capacity_check(gpus, "GPU-1", [], candidate_gb=20.0)
    assert ok is True and used == 0.0


# ---------------------------------------------------------------------------
# Task 4: reconcile from .env + gpu-assignments.yml
# ---------------------------------------------------------------------------

def test_reconcile_seeds_chat_and_embed(tmp_path):
    (tmp_path / ".env").write_text(
        "LLAMACPP_MODEL=qwen.gguf\nLLAMACPP_EMBED_MODEL=nomic.gguf\nLLAMACPP_CTX_SIZE=131072\n")
    (tmp_path / "gpu-assignments.yml").write_text(
        "services:\n  llamacpp:\n    deploy:\n      resources:\n        reservations:\n"
        "          devices:\n            - device_ids: ['GPU-xyz']\n")
    reg = _reg(tmp_path)
    reg.reconcile()
    models = reg.list_models()
    assert models["local-chat"].source["file"] == "qwen.gguf"
    assert models["local-chat"].gpu_uuid == "GPU-xyz"
    assert models["local-chat"].config["ctx"] == 131072
    assert models["local-embed"].source["file"] == "nomic.gguf"
    assert models["local-chat"].enabled is True

def test_reconcile_is_idempotent_and_preserves_intent(tmp_path):
    (tmp_path / ".env").write_text("LLAMACPP_MODEL=qwen.gguf\n")
    reg = _reg(tmp_path)
    reg.reconcile()
    rec = reg.get("local-chat"); rec.est_vram_gb = 22.0; reg.upsert(rec)
    reg.reconcile()
    assert reg.get("local-chat").est_vram_gb == 22.0


def test_reconcile_preserves_operator_intent_on_existing(tmp_path):
    (tmp_path / ".env").write_text("LLAMACPP_MODEL=qwen.gguf\nLLAMACPP_CTX_SIZE=131072\n")
    reg = _reg(tmp_path)
    reg.reconcile()
    rec = reg.get("local-chat")
    rec.config["ctx"] = 262144
    rec.source["file"] = "operator-pick.gguf"
    rec.gpu_uuid = "GPU-operator"
    rec.enabled = False
    reg.upsert(rec)
    reg.reconcile()  # must NOT clobber any of the operator-set fields
    after = reg.get("local-chat")
    assert after.config["ctx"] == 262144
    assert after.source["file"] == "operator-pick.gguf"
    assert after.gpu_uuid == "GPU-operator"
    assert after.enabled is False


def test_reconcile_handles_quoted_env_values(tmp_path):
    (tmp_path / ".env").write_text('LLAMACPP_MODEL="qwen.gguf"\n')
    reg = _reg(tmp_path)
    reg.reconcile()
    assert reg.get("local-chat").source["file"] == "qwen.gguf"
