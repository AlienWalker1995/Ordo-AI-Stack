"""The source a command reads when `--source` is not given.

The operator's live source is `<out>/ordo.yaml` (default `out/ordo.yaml`, the directory `--out` names).
A bare `ordo doctor` / `ordo parity` / `ordo preflight` used to read the public `ordo.example.yaml`
instead, so the checks validated the example rather than the config the stack runs. The rule: an
absent `--source` means `<out>/ordo.yaml` when it exists, else the example (a fresh checkout before
`ordo init`), and the command states on stderr which source it read.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ordo import cli
from ordo.host import doctor

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "ordo.example.yaml"

# A minimal operator source with a site key the example does not carry (the example is `site: {}`).
OPERATOR_ORDO_YAML = """\
hardware: {gpus: [{name: op-gpu, vram_gb: 32}], ram_gb: 128, cpu_cores: 32, platform: Linux}
model: auto
tier: auto
site:
  BASE_PATH: /srv/operator/ordo
"""


@pytest.fixture
def no_docker_probes(monkeypatch):
    """`ordo doctor` also inspects the running containers and the materialized alert-delivery secrets;
    the source resolution is what is under test."""
    monkeypatch.setattr(doctor, "substrate_check", lambda project: (True, "substrate: stubbed"))
    monkeypatch.setattr(doctor, "open_webui_check", lambda project: (True, "open-webui: stubbed"))
    monkeypatch.setattr(doctor, "alerting_check", lambda compose, out_dir: (True, "alerting: stubbed"))
    monkeypatch.setattr(doctor, "cpu_fallback_check", lambda project, threads: (True, "cpu-fallback: stubbed"))


def _operator_checkout(root: Path) -> Path:
    """A working directory whose out/ holds the operator's live source."""
    out = root / "out"
    out.mkdir()
    live = out / "ordo.yaml"
    live.write_text(OPERATOR_ORDO_YAML, encoding="utf-8")
    return live


def test_bare_doctor_validates_the_live_source(tmp_path, monkeypatch, capsys, no_docker_probes):
    live = _operator_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["doctor"]) == 0

    captured = capsys.readouterr()
    assert f"source '{Path('out') / 'ordo.yaml'}': valid" in captured.out
    assert "ordo.example.yaml" not in captured.out
    assert "--source not given" in captured.err
    assert live.exists()


def test_bare_doctor_on_a_fresh_checkout_falls_back_to_the_example(tmp_path, monkeypatch, capsys,
                                                                    no_docker_probes):
    monkeypatch.chdir(tmp_path)  # no out/ordo.yaml yet: before `ordo init`

    assert cli.main(["doctor"]) == 0

    captured = capsys.readouterr()
    assert f"source '{EXAMPLE}': valid" in captured.out
    assert "ordo.example.yaml" in captured.err


def test_explicit_source_is_honoured_and_not_announced(tmp_path, monkeypatch, capsys, no_docker_probes):
    _operator_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["--source", str(EXAMPLE), "doctor"]) == 0

    captured = capsys.readouterr()
    assert f"source '{EXAMPLE}': valid" in captured.out
    assert "--source not given" not in captured.err


def test_bare_parity_renders_the_live_source(tmp_path, monkeypatch, capsys):
    """The operator's BASE_PATH is in the render only when the live source is the one read."""
    _operator_checkout(tmp_path)
    ref = tmp_path / "ref.env"
    ref.write_text("BASE_PATH=/srv/operator/ordo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["parity", "--ref", str(ref)]) == 0
    stdout = capsys.readouterr().out
    assert "compared 1 key(s)" in stdout  # the example renders no BASE_PATH: it would compare 0
    assert "PARITY OK" in stdout


def test_bare_preflight_reads_the_source_in_its_out_dir(tmp_path, monkeypatch, capsys):
    """A command with --out resolves the source from that directory, not from ./out."""
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "ordo.yaml").write_text(OPERATOR_ORDO_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cli.main(["preflight", "--out", str(stack), "--no-host", "--no-images"])

    assert f"source: {stack / 'ordo.yaml'}" in capsys.readouterr().err
