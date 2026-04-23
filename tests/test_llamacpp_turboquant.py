"""Shell-wrapper assertions for the llama-server entrypoint when TurboQuant KV types are selected.

The wrapper lives at scripts/llamacpp/run-llama-server.sh and assembles the llama-server arg vector
from LLAMACPP_* env vars. TurboQuant cache types (turbo2, turbo3) require Flash Attention to be on
or the kernels silently produce garbage — see docs/configuration.md. These tests pin the
behavior that makes that mistake impossible."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "llamacpp" / "run-llama-server.sh"


def _sh() -> str:
    """Return a POSIX shell executable. Skip on Windows without one on PATH."""
    for candidate in ("sh", "bash"):
        path = shutil.which(candidate)
        if path:
            return path
    pytest.skip("POSIX sh not available on PATH")
    return ""  # unreachable — keeps type-checkers happy


def _run_wrapper(env: dict[str, str]) -> str:
    """Exec the wrapper with `/app/llama-server` stubbed to `echo`. Return captured stdout."""
    full_env = {**os.environ, **env}
    # The wrapper calls `exec /app/llama-server "$@"` on the last line. We can't rewrite
    # that hard-coded path safely from Python for every shell; instead we patch the wrapper's
    # exec target via a thin tempdir shim that prepends a fake /app/llama-server (echo) to PATH.
    # But `exec /app/...` uses an absolute path, so PATH manipulation does not apply.
    # Simplest reliable approach: read the wrapper, substitute `exec /app/llama-server` with
    # `echo`, and pipe through `sh` on stdin.
    script = WRAPPER.read_text(encoding="utf-8")
    script = script.replace("exec /app/llama-server", "echo FINAL_ARGS:")
    result = subprocess.run(
        [_sh()],
        input=script,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"wrapper failed: rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


def _base_env(**overrides: str) -> dict[str, str]:
    """Minimal env so the wrapper's default-arg section doesn't reference undefined vars."""
    env = {
        "LLAMACPP_MODEL": "fake.gguf",
        "LLAMACPP_CTX_SIZE": "131072",
        "LLAMACPP_PARALLEL": "1",
        "LLAMACPP_ROPE_SCALING": "none",
        "LLAMACPP_ROPE_SCALE": "1",
        "LLAMACPP_YARN_ORIG_CTX": "0",
        "LLAMACPP_GPU_LAYERS": "-1",
        "LLAMACPP_FLASH_ATTN": "auto",
        "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION": "0",
        "LLAMACPP_KV_CACHE_TYPE_K": "",
        "LLAMACPP_KV_CACHE_TYPE_V": "",
        "LLAMACPP_OVERRIDE_KV": "",
        "LLAMACPP_EXTRA_ARGS": "",
    }
    env.update(overrides)
    return env


def _final_args(stdout: str) -> str:
    """Extract the FINAL_ARGS line emitted by the stubbed exec."""
    for line in stdout.splitlines():
        if line.startswith("FINAL_ARGS:"):
            return line[len("FINAL_ARGS:") :].strip()
    raise AssertionError(f"no FINAL_ARGS line in output:\n{stdout}")


def test_wrapper_syntax_is_posix_sh() -> None:
    """Wrapper must parse cleanly under `sh -n` (no bash-isms or syntax slips)."""
    result = subprocess.run(
        [_sh(), "-n", str(WRAPPER)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"sh -n failed:\n{result.stderr}"


def test_tbqp3_0_forces_flash_attention_on() -> None:
    """When K or V cache type is any tbq* variant, --flash-attn on must be appended
    regardless of LLAMACPP_FLASH_ATTN. TurboQuant kernels silently corrupt without FA."""
    env = _base_env(
        LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION="1",
        LLAMACPP_KV_CACHE_TYPE_K="tbqp3_0",
        LLAMACPP_KV_CACHE_TYPE_V="tbqp3_0",
        LLAMACPP_FLASH_ATTN="off",  # deliberately hostile
    )
    args = _final_args(_run_wrapper(env))
    assert "--cache-type-k tbqp3_0" in args, args
    assert "--cache-type-v tbqp3_0" in args, args
    # The guard must append `--flash-attn on` AFTER any earlier --flash-attn arg so
    # llama-server takes the safe value (last-wins).
    last_flash_attn_value = args.rsplit("--flash-attn", 1)[-1].strip().split()[0]
    assert last_flash_attn_value == "on", (
        f"expected last --flash-attn value to be 'on', got {last_flash_attn_value!r}\nargs={args}"
    )


def test_tbq4_0_also_forces_flash_attention_on() -> None:
    """tbq4_0 (4-bit TurboQuant) has the same FA requirement as the packed variants."""
    env = _base_env(
        LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION="1",
        LLAMACPP_KV_CACHE_TYPE_K="tbq4_0",
        LLAMACPP_KV_CACHE_TYPE_V="tbq4_0",
        LLAMACPP_FLASH_ATTN="auto",
    )
    args = _final_args(_run_wrapper(env))
    last_flash_attn_value = args.rsplit("--flash-attn", 1)[-1].strip().split()[0]
    assert last_flash_attn_value == "on", args


def test_mixed_k_tbq_v_q4_0_still_forces_flash_attention() -> None:
    """If only ONE of K/V is a tbq* type (asymmetric config), FA is still required
    because the tbq* side's rotation-quantize kernels need it. The wrapper's guard
    fires whenever either K or V matches tbq*."""
    env = _base_env(
        LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION="1",
        LLAMACPP_KV_CACHE_TYPE_K="tbqp3_0",
        LLAMACPP_KV_CACHE_TYPE_V="q4_0",
        LLAMACPP_FLASH_ATTN="off",
    )
    args = _final_args(_run_wrapper(env))
    last_flash_attn_value = args.rsplit("--flash-attn", 1)[-1].strip().split()[0]
    assert last_flash_attn_value == "on", args


def test_q4_0_does_not_force_flash_attention() -> None:
    """Non-turbo cache types must not get the safety-rail flip — operator's
    LLAMACPP_FLASH_ATTN setting stays authoritative."""
    env = _base_env(
        LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION="1",
        LLAMACPP_KV_CACHE_TYPE_K="q4_0",
        LLAMACPP_KV_CACHE_TYPE_V="q4_0",
        LLAMACPP_FLASH_ATTN="auto",
    )
    args = _final_args(_run_wrapper(env))
    # Only one --flash-attn arg, and its value is the operator's setting (`auto`).
    assert args.count("--flash-attn") == 1, args
    flash_value = args.split("--flash-attn", 1)[1].strip().split()[0]
    assert flash_value == "auto", args


def test_quantization_disabled_emits_no_cache_type_args() -> None:
    """With LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION=0, --cache-type-* must not appear
    regardless of the type env vars, and the FA safety rail must not fire."""
    env = _base_env(
        LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION="0",
        LLAMACPP_KV_CACHE_TYPE_K="tbqp3_0",  # should be ignored
        LLAMACPP_KV_CACHE_TYPE_V="tbqp3_0",
        LLAMACPP_FLASH_ATTN="auto",
    )
    args = _final_args(_run_wrapper(env))
    assert "--cache-type-k" not in args, args
    assert "--cache-type-v" not in args, args
    # Exactly one --flash-attn, value unchanged (quant disabled → no safety rail).
    assert args.count("--flash-attn") == 1, args
    flash_value = args.split("--flash-attn", 1)[1].strip().split()[0]
    assert flash_value == "auto", args
