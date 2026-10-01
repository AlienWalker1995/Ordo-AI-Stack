"""model-gateway routes local-chat to whichever engine the GPU chat service runs.

NInfer accepts a request only when its `model` equals the server's model id, and the GPU launcher
sets that id to the weights file (run-ninfer-serve.sh). So the gateway's GPU deployments send the
GPU weights file as `model`; llama.cpp ignores the field, so one rule serves both engines. The pin
alias name and the vision flag also follow the weights file and the rendered vision key."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "services" / "model-gateway" / "entrypoint.sh"
TEMPLATE = ROOT / "services" / "model-gateway" / "litellm_config.yaml"


def _sh() -> str:
    for candidate in ("sh", "bash"):
        path = shutil.which(candidate)
        if path:
            return path
    pytest.skip("POSIX sh not available on PATH")
    return ""


def _render_config(tmp_path: Path, **env_overrides: str) -> dict:
    out_path = tmp_path / "config.yaml"
    script = ENTRYPOINT.read_text(encoding="utf-8")
    script = script.replace("/app/config.template.yaml", TEMPLATE.resolve().as_posix())
    script = script.replace("/tmp/config.yaml", out_path.as_posix())
    script = script.replace("/app/secret-env.sh", (ENTRYPOINT.parent / "secret-env.sh").resolve().as_posix())
    script = script[: script.index("cp /app/throughput_callback.py")]
    key_file = tmp_path / "litellm_master_key"
    key_file.write_text("sk-" + "a" * 32, encoding="utf-8")
    env = {**{k: v for k, v in os.environ.items() if k != "LITELLM_MASTER_KEY"},
           "LITELLM_MASTER_KEY_FILE": key_file.as_posix(),
           "LLAMACPP_CTX_SIZE": "262144", "LLAMACPP_N_PREDICT": "65536", "LLAMACPP_CPU_CTX": "262144",
           "LLAMACPP_MODEL": "qwen3_8_27b_nvfp4.ninfer", "LLAMACPP_CPU_MODEL": "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
           "LLAMACPP_EMBED_MODEL": "nomic-embed-text-v1.5.Q4_K_M.gguf", "LLAMACPP_IMAGE": "ordo/ninfer:abc",
           "LLAMACPP_MMPROJ": "", "LOCAL_INPUT_COST_PER_TOKEN": "0", "LOCAL_OUTPUT_COST_PER_TOKEN": "0",
           **env_overrides}
    result = subprocess.run([_sh()], input=script, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    text = out_path.read_text(encoding="utf-8")
    assert not re.search(r"__[A-Z_]+__", text), text
    return yaml.safe_load(text)


def _deployment(cfg: dict, name: str) -> dict:
    return next(m for m in cfg["model_list"] if m["model_name"] == name)


def test_the_gpu_deployments_send_the_weights_file_as_the_model(tmp_path):
    cfg = _render_config(tmp_path)
    for name in ("local-chat", "qwen3_8_27b_nvfp4"):
        params = _deployment(cfg, name)["litellm_params"]
        assert params["model"] == "openai/qwen3_8_27b_nvfp4.ninfer", name
        assert params["api_base"] == "http://llamacpp:8080/v1"


def test_a_gguf_model_keeps_its_alias_and_routes_the_same_way(tmp_path):
    cfg = _render_config(tmp_path, LLAMACPP_MODEL="Qwen3.8-27B-UD-Q6_K.gguf")
    assert _deployment(cfg, "qwen3.8-27b-ud-q6_k")["litellm_params"]["model"] == "openai/Qwen3.8-27B-UD-Q6_K.gguf"


def test_the_pin_alias_drops_the_ninfer_extension(tmp_path):
    names = {m["model_name"] for m in _render_config(tmp_path)["model_list"]}
    assert "qwen3_8_27b_nvfp4" in names and "qwen3_8_27b_nvfp4.ninfer" not in names


def test_vision_follows_the_rendered_ninfer_vision_key(tmp_path):
    def vision(cfg: dict) -> bool:
        return bool(_deployment(cfg, "local-chat")["model_info"].get("supports_vision"))
    assert vision(_render_config(tmp_path)) is False
    assert vision(_render_config(tmp_path, LLAMACPP_VISION="1")) is True
    assert vision(_render_config(tmp_path, LLAMACPP_MODEL="m.gguf", LLAMACPP_MMPROJ="/models/p.gguf")) is True
