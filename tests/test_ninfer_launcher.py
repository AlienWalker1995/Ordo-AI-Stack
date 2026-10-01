"""scripts/llamacpp/run-ninfer-serve.sh: the GPU chat service's launcher for a `backend: ninfer` model. It
turns the rendered LLAMACPP_* env into the ninfer-serve argv; the exec is stubbed to echo."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "llamacpp" / "run-ninfer-serve.sh"
ENV = {"LLAMACPP_MODEL": "qwen3_8_27b_nvfp4.ninfer", "LLAMACPP_CTX_SIZE": "262144", "LLAMACPP_N_PREDICT": "65536",
       "LLAMACPP_REASONING_BUDGET": "32768",
       "LLAMACPP_EXTRA_ARGS": "--max-concurrency 1 --kv-dtype fp8 --spec mtp --draft-tokens 3 --lm-head-draft",
       # llama.cpp-only keys the service also receives: they must not leak into the argv
       "LLAMACPP_GPU_LAYERS": "-1", "LLAMACPP_FLASH_ATTN": "auto", "LLAMACPP_MMPROJ": "",
       "LLAMACPP_KV_CACHE_TYPE_K": "q8_0"}


def _sh() -> str:
    for candidate in ("sh", "bash"):
        path = shutil.which(candidate)
        if path:
            return path
    pytest.skip("POSIX sh not available on PATH")
    return ""


def _run(**overrides: str) -> subprocess.CompletedProcess:
    script = LAUNCHER.read_text(encoding="utf-8").replace("exec /usr/local/bin/ninfer-serve", "echo FINAL_ARGS:")
    env = {**os.environ, **ENV, **overrides}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
    return subprocess.run([_sh()], input=script, env=env, capture_output=True, text=True, timeout=10)


def _args(result: subprocess.CompletedProcess) -> list[str]:
    assert result.returncode == 0, result.stderr
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("FINAL_ARGS:"))
    return line[len("FINAL_ARGS:"):].split()


def test_the_launcher_parses_as_posix_sh() -> None:
    assert subprocess.run([_sh(), "-n", str(LAUNCHER)], capture_output=True).returncode == 0


def test_the_artifact_window_and_model_id_come_from_the_render() -> None:
    args = _args(_run())
    assert args[0] == "/models/qwen3_8_27b_nvfp4.ninfer"
    joined = " ".join(args)
    # the public model id is the artifact file: the gateway sends it as `model`, the dashboard reads it
    assert "--model-id qwen3_8_27b_nvfp4.ninfer" in joined
    # one request gets the whole window: the KV pool equals the per-request ceiling
    assert "--max-context 262144 --kv-capacity 262144" in joined
    assert "--default-max-tokens 65536" in joined and "--default-thinking-budget 32768" in joined
    assert "--host 0.0.0.0" in joined and "--port 8080" in joined
    assert "--spec mtp --draft-tokens 3 --lm-head-draft" in joined


def test_llamacpp_only_settings_and_metrics_never_reach_ninfer() -> None:
    args = _args(_run())
    for flag in ("--metrics", "--n-gpu-layers", "--flash-attn", "--mmproj", "--cache-type-k", "--vision"):
        assert flag not in args, args


def test_vision_is_loaded_only_when_rendered_on() -> None:
    assert "--vision" in _args(_run(LLAMACPP_VISION="1"))
    assert "--vision" not in _args(_run(LLAMACPP_VISION="0"))


def test_a_missing_window_refuses_to_start() -> None:
    result = _run(LLAMACPP_CTX_SIZE=None)
    assert result.returncode != 0 and "LLAMACPP_CTX_SIZE" in result.stderr
    assert "FINAL_ARGS:" not in result.stdout
