"""Parity check: does the render engine reproduce an existing .env?

This is merge-gate (a) — "renders today's stack from one source, zero hand-edits." Point it at
the live stack's .env (read-only) and it reports any key where the rendered value differs.
Only compares keys the renderer actually produces AND that exist in the reference (so unrelated
keys in a real .env don't count as failures).
"""
from __future__ import annotations

from pathlib import Path


def load_env(path: str | Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def diff(rendered: dict[str, str], reference: dict[str, str]) -> dict[str, dict[str, str]]:
    """Return {key: {rendered, reference}} for every rendered key that the reference also
    defines but with a different value. Empty dict == parity."""
    out: dict[str, dict[str, str]] = {}
    for k, rv in rendered.items():
        if k in reference and str(reference[k]) != str(rv):
            out[k] = {"rendered": str(rv), "reference": str(reference[k])}
    return out


def report(rendered: dict[str, str], reference_path: str | Path) -> tuple[bool, dict, list[str]]:
    """Returns (parity_ok, mismatches, keys_compared)."""
    ref = load_env(reference_path)
    compared = [k for k in rendered if k in ref]
    mism = diff(rendered, ref)
    return (len(mism) == 0, mism, compared)
