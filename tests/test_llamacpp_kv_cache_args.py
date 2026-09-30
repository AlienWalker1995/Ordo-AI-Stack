"""Shell-wrapper assertions for the llama-server entrypoint's KV-cache quantization flags.

The wrapper lives at scripts/llamacpp/run-llama-server.sh and assembles the llama-server arg vector
from LLAMACPP_* env vars. These pin that the cache-type flags appear only when quantization is on."""

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


def test_q4_0_does_not_force_flash_attention() -> None:
    """Quantized cache types never force Flash Attention on: the operator's
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
        LLAMACPP_KV_CACHE_TYPE_K="q8_0",  # should be ignored
        LLAMACPP_KV_CACHE_TYPE_V="q8_0",
        LLAMACPP_FLASH_ATTN="auto",
    )
    args = _final_args(_run_wrapper(env))
    assert "--cache-type-k" not in args, args
    assert "--cache-type-v" not in args, args
    # Exactly one --flash-attn, value unchanged (quant disabled → no safety rail).
    assert args.count("--flash-attn") == 1, args
    flash_value = args.split("--flash-attn", 1)[1].strip().split()[0]
    assert flash_value == "auto", args


def test_missing_context_window_refuses_to_start() -> None:
    """The window is computed by the render. Without it the wrapper exits instead of starting
    llama-server on a window the render never chose (it used to fall back to 262144)."""
    env = {k: v for k, v in {**os.environ, **_base_env()}.items() if k != "LLAMACPP_CTX_SIZE"}
    script = WRAPPER.read_text(encoding="utf-8").replace("exec /app/llama-server", "echo FINAL_ARGS:")
    result = subprocess.run([_sh()], input=script, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0, result.stdout
    assert "FINAL_ARGS:" not in result.stdout
    assert "LLAMACPP_CTX_SIZE" in result.stderr


def _final_args_with_help(help_text: str) -> str:
    """Run the wrapper with the binary's `--help` stubbed to print `help_text` (the probe that picks
    the no-mmap flag) and the final exec stubbed to echo, as _run_wrapper does."""
    script = WRAPPER.read_text(encoding="utf-8")
    script = script.replace("/app/llama-server --help", f"printf '%s\n' '{help_text}'")
    script = script.replace("exec /app/llama-server", "echo FINAL_ARGS:")
    result = subprocess.run([_sh()], input=script, env={**os.environ, **_base_env()}, capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return _final_args(result.stdout)


def test_a_v0_5_build_loads_without_mmap_through_load_mode() -> None:
    """llama.cpp v0.5.0 (the patched image) dropped --no-mmap for --load-mode; passing the old flag
    makes llama-server exit with 'invalid argument: --no-mmap'."""
    args = _final_args_with_help("-lm,    --load-mode MODE            model loading mode (default: auto)")
    assert "--load-mode none" in args, args
    assert "--no-mmap" not in args, args


def test_an_older_build_keeps_no_mmap() -> None:
    """The pinned upstream images (b9935) have no --load-mode and still take --no-mmap."""
    args = _final_args_with_help("--mmap, --no-mmap                       whether to memory-map model")
    assert "--no-mmap" in args, args
    assert "--load-mode" not in args, args


V05_HELP = "-lm,    --load-mode MODE            model loading mode (default: auto)"


def _run_with_help(help_text: str, **env: str) -> subprocess.CompletedProcess:
    script = WRAPPER.read_text(encoding="utf-8")
    script = script.replace("/app/llama-server --help", f"printf '%s\n' '{help_text}'")
    script = script.replace("exec /app/llama-server", "echo FINAL_ARGS:")
    return subprocess.run([_sh()], input=script, env={**os.environ, **_base_env(**env)}, capture_output=True,
                          text=True, timeout=10)


def test_a_lazy_mmap_model_memory_maps_and_reads_its_table_on_demand() -> None:
    result = _run_with_help(V05_HELP, LLAMACPP_LOAD_MODE="mmap-lazy")
    args = _final_args(result.stdout)
    assert "--load-mode mmap --lazy-mode on" in args, args
    assert "--load-mode none" not in args and "--no-mmap" not in args, args


def test_lazy_mmap_on_a_build_without_it_refuses_to_start() -> None:
    result = _run_with_help("--mmap, --no-mmap   whether to memory-map model", LLAMACPP_LOAD_MODE="mmap-lazy")
    assert result.returncode != 0
    assert "FINAL_ARGS:" not in result.stdout


def test_an_unknown_load_mode_refuses_to_start() -> None:
    result = _run_with_help(V05_HELP, LLAMACPP_LOAD_MODE="mlock")
    assert result.returncode != 0 and "LLAMACPP_LOAD_MODE" in result.stderr


def test_offloaded_experts_pin_the_placement_and_the_threads() -> None:
    args = _final_args(_run_with_help(V05_HELP, LLAMACPP_N_CPU_MOE="15", LLAMACPP_THREADS="12").stdout)
    assert "--n-cpu-moe 15 --fit off" in args, args
    assert "--threads 12" in args, args


def test_no_placement_keys_add_no_placement_flags() -> None:
    args = _final_args(_run_with_help(V05_HELP).stdout)
    for flag in ("--n-cpu-moe", "--fit", "--threads", "--lazy-mode"):
        assert flag not in args, args
