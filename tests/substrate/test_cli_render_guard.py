"""The bare-`ordo render` anti-clobber guard.

Root cause of the 2026-07-15 SSO outage: `--source` defaults to `ordo.example.yaml`, so a bare
`ordo render` on the operator box rendered the PUBLIC EXAMPLE into `./out` and stripped the
operator's host-paths (BASE_PATH/DATA_PATH/…) out of `.env` → the oauth2-proxy allowlist mount
resolved to an empty fabricated dir → zero-email allowlist → deny-all.

The guard: when `--source` is NOT passed explicitly AND the target `--out` already holds an
`ordo.yaml` that DIFFERS from the example, render from that existing file (the operator's real
source), not the example — unless `--force` is given. An explicit `--source` is always honoured
(CI passes `--source ordo.example.yaml`).
"""
from __future__ import annotations

from pathlib import Path

from ordo import cli

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "ordo.example.yaml"

# A minimal operator source: a real host BASE_PATH the example does NOT carry (example is `site: {}`).
OPERATOR_ORDO_YAML = """\
hardware: {gpus: [{name: op-gpu, vram_gb: 32}], ram_gb: 128, cpu_cores: 32, platform: Linux}
model: auto
tier: auto
plugins: [edge]
site:
  BASE_PATH: /srv/operator/ordo
  DATA_PATH: /srv/operator/ordo/data
"""


def _read_env(out: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (out / ".env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k] = v
    return env


def test_bare_render_into_populated_outdir_does_not_clobber_with_example(tmp_path, capsys):
    """No `--source`: an out-dir holding a differing ordo.yaml is rendered FROM, so the operator's
    BASE_PATH survives (the example, `site: {}`, would have dropped it — the outage)."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "ordo.yaml").write_text(OPERATOR_ORDO_YAML, encoding="utf-8")

    rc = cli.main(["render", "--out", str(out)])
    assert rc == 0

    env = _read_env(out)
    assert env.get("BASE_PATH") == "/srv/operator/ordo", (
        "bare render clobbered the operator source with the example — BASE_PATH lost")
    assert "differs from the example" in capsys.readouterr().out


def test_explicit_example_source_still_renders(tmp_path):
    """CI path: an EXPLICIT `--source ordo.example.yaml` is honoured even when out/ has its own
    ordo.yaml — source_explicit short-circuits the guard. The example has no BASE_PATH."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "ordo.yaml").write_text(OPERATOR_ORDO_YAML, encoding="utf-8")

    rc = cli.main(["--source", str(EXAMPLE), "render", "--out", str(out)])
    assert rc == 0
    assert "BASE_PATH" not in _read_env(out)  # rendered the example, not the operator source


def test_force_renders_example_over_populated_outdir(tmp_path):
    """`--force` is the explicit escape hatch: render the default/example even though out/ holds a
    differing ordo.yaml (operator opts in to the clobber)."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "ordo.yaml").write_text(OPERATOR_ORDO_YAML, encoding="utf-8")

    rc = cli.main(["render", "--out", str(out), "--force"])
    assert rc == 0
    assert "BASE_PATH" not in _read_env(out)  # forced example render, operator source ignored


def test_bare_render_into_empty_outdir_uses_example(tmp_path):
    """No out/ordo.yaml → nothing to preserve → the example default renders fine (unchanged behaviour
    for a fresh checkout / first render)."""
    out = tmp_path / "out"
    rc = cli.main(["render", "--out", str(out)])
    assert rc == 0
    assert (out / ".env").exists()
