"""The substrate digest ties ops-controller's baked render inputs to the render that wrote out/.

ops-controller ships its own copy of ordo/, catalog/ and the services/ manifests, and re-renders
out/ from that copy on a model switch or plugin toggle. When the image is older than the checkout
that last rendered out/, that re-render silently reverts newer manifest changes (live, 2026-09-24:
the running image would have dropped #240's oauth2-proxy CSRF flags). Every render now records
the digest of its inputs in out/manifest.json, and ops-controller refuses to re-render over a
render made from different inputs.
"""
from __future__ import annotations

import builtins
import io
import json
import shutil
from pathlib import Path

import pytest
import yaml

from ordo import cli, doctor, substrate
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.control import ControlPlane
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
SOURCE = {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto"}


def _copy_substrate(dest: Path) -> Path:
    """A second checkout holding exactly the digest inputs, laid out like the repo."""
    for path in substrate.substrate_files(ROOT):
        target = dest / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return dest


# --- the digest itself ---

def test_digest_is_stable_across_runs_and_checkouts(tmp_path):
    first = substrate.substrate_digest(ROOT)
    assert first == substrate.substrate_digest(ROOT)
    assert first == substrate.substrate_digest(_copy_substrate(tmp_path / "copy"))


def test_digest_covers_code_catalog_and_every_manifest_kind():
    names = {p.relative_to(ROOT).as_posix() for p in substrate.substrate_files(ROOT)}
    assert "ordo/render.py" in names
    assert "catalog/models.yaml" in names
    assert "services/edge/plugin.yaml" in names
    assert "services/hermes/agent.yaml" in names
    assert "services/dashboard/dashboard.yaml" in names
    assert "services/model-gateway/litellm_config.yaml" in names
    assert any(n.endswith("/catalog.json") for n in names)


def test_digest_changes_when_a_manifest_changes(tmp_path):
    copy = _copy_substrate(tmp_path / "copy")
    before = substrate.substrate_digest(copy)
    manifest = copy / "services" / "edge" / "plugin.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    assert substrate.substrate_digest(copy) != before


def test_digest_changes_when_a_manifest_is_added(tmp_path):
    copy = _copy_substrate(tmp_path / "copy")
    before = substrate.substrate_digest(copy)
    (copy / "services" / "new-thing").mkdir()
    (copy / "services" / "new-thing" / "plugin.yaml").write_text("id: new-thing\n", encoding="utf-8")
    assert substrate.substrate_digest(copy) != before


def test_digest_ignores_caches_line_endings_and_non_render_files(tmp_path):
    copy = _copy_substrate(tmp_path / "copy")
    before = substrate.substrate_digest(copy)
    (copy / "ordo" / "__pycache__").mkdir()
    (copy / "ordo" / "__pycache__" / "render.cpython-311.pyc").write_bytes(b"\x00compiled")
    (copy / "services" / "edge" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    manifest = copy / "services" / "edge" / "plugin.yaml"
    manifest.write_bytes(manifest.read_bytes().replace(b"\n", b"\r\n"))
    assert substrate.substrate_digest(copy) == before


def test_render_reads_nothing_outside_the_digest(tmp_path, monkeypatch):
    """If render starts reading a new file, the digest must cover it, or a stale image hides."""
    covered = {p.resolve() for p in substrate.substrate_files(ROOT)}
    read: set[Path] = set()
    real_open = builtins.open

    def tracing_open(file, *args, **kwargs):
        if isinstance(file, str | Path):
            read.add(Path(file).resolve())
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracing_open)
    monkeypatch.setattr(io, "open", tracing_open)  # pathlib's read_text opens through io.open
    render(Source.from_dict(SOURCE), CATALOG).write(tmp_path / "out")
    monkeypatch.undo()
    repo_reads = {p for p in read if p.is_relative_to(ROOT.resolve())}
    assert repo_reads, "the trace saw no repo reads, so it proves nothing"
    assert repo_reads <= covered, sorted(str(p) for p in repo_reads - covered)


def test_image_ships_every_digest_input():
    """The digest is computed inside the image too, so each input must be in the build context."""
    allowed = {line[1:].rstrip("/") for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
               if line.startswith("!")}
    tops = {p.relative_to(ROOT).parts[0] for p in substrate.substrate_files(ROOT)}
    assert tops <= allowed


# --- every render records it ---

def test_render_records_the_digest_in_the_manifest(tmp_path):
    rc = render(Source.from_dict(SOURCE), CATALOG)
    rc.write(tmp_path / "out")
    written = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert written["substrate_digest"] == substrate.current_digest()
    assert written["substrate_digest"] == substrate.substrate_digest(ROOT)


# --- ops-controller refuses to render over a render made from other inputs ---

def _cp(tmp_path, plugins="auto", recorded: str | None = "unset"):
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump({**SOURCE, "plugins": plugins}, sort_keys=False), encoding="utf-8")
    out = tmp_path / "out"
    if recorded != "unset":
        out.mkdir()
        manifest = {"tier": "ultra"} if recorded is None else {"substrate_digest": recorded}
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return ControlPlane(src, CATALOG, REGISTRY, out), src


def _recorded(tmp_path) -> str | None:
    return json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8")).get("substrate_digest")


def test_model_switch_is_409_when_the_last_render_used_other_inputs(tmp_path):
    cp, src = _cp(tmp_path, recorded="0" * 64)
    before = src.read_text(encoding="utf-8")
    code, body = cp.route("POST", "/model-config", {"model": CATALOG.models[0].id})
    assert code == 409
    assert "ordo recreate ops-controller" in body["error"]
    assert body["substrate_digest"] == cp.substrate_digest
    assert body["rendered_substrate_digest"] == "0" * 64
    assert src.read_text(encoding="utf-8") == before       # source untouched
    assert _recorded(tmp_path) == "0" * 64                # out/ untouched


def test_model_switch_proceeds_when_the_digests_match(tmp_path):
    cp, _ = _cp(tmp_path, recorded=substrate.current_digest())
    code, body = cp.route("POST", "/model-config", {"model": CATALOG.models[0].id})
    assert code == 200 and body["ok"]


@pytest.mark.parametrize("recorded", [None, "unset"], ids=["pre-upgrade manifest", "no manifest"])
def test_model_switch_allowed_and_records_when_nothing_recorded(tmp_path, recorded):
    cp, _ = _cp(tmp_path, recorded=recorded)
    code, _ = cp.route("POST", "/model-config", {"model": CATALOG.models[0].id})
    assert code == 200
    assert _recorded(tmp_path) == cp.substrate_digest


def test_plugin_enable_is_409_on_mismatch(tmp_path):
    cp, src = _cp(tmp_path, plugins=["comfyui"], recorded="0" * 64)
    before = src.read_text(encoding="utf-8")
    code, body = cp.route("POST", "/plugins/open-webui/enable", {})
    assert code == 409 and "ordo recreate ops-controller" in body["error"]
    assert src.read_text(encoding="utf-8") == before


def test_plugin_disable_is_409_on_mismatch(tmp_path):
    cp, src = _cp(tmp_path, plugins=["comfyui", "rag"], recorded="0" * 64)
    before = src.read_text(encoding="utf-8")
    code, body = cp.route("POST", "/plugins/rag/disable", {})
    assert code == 409 and "ordo recreate ops-controller" in body["error"]
    assert src.read_text(encoding="utf-8") == before


def test_plugin_enable_proceeds_on_match(tmp_path):
    cp, _ = _cp(tmp_path, plugins=["comfyui"], recorded=substrate.current_digest())
    code, body = cp.route("POST", "/plugins/open-webui/enable", {})
    assert code == 200 and body["ok"] and not body["already_rendered"]


def test_health_reports_the_digest(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("GET", "/health")
    assert code == 200
    assert body == {"ok": True, "substrate_digest": substrate.current_digest()}


# --- ordo doctor compares the running ops-controller with the checkout ---

def _doctor(monkeypatch, capsys, running):
    def fake_read(project):
        assert project == "ordo"
        if isinstance(running, Exception):
            raise running
        return running

    monkeypatch.setattr(doctor, "read_running_substrate_digest", fake_read)
    monkeypatch.setattr(doctor, "read_open_webui_probe", lambda project: None)
    code = cli.main(["doctor"])
    return code, capsys.readouterr().out


def test_doctor_flags_a_mismatched_ops_controller(monkeypatch, capsys):
    code, out = _doctor(monkeypatch, capsys, "0" * 64)
    assert code == 1
    assert "MISMATCH" in out and "ordo recreate ops-controller" in out


def test_doctor_flags_an_ops_controller_that_predates_the_digest(monkeypatch, capsys):
    code, out = _doctor(monkeypatch, capsys, "")
    assert code == 1 and "MISMATCH" in out


def test_doctor_passes_when_the_digests_match(monkeypatch, capsys):
    code, out = _doctor(monkeypatch, capsys, substrate.current_digest())
    assert code == 0
    assert "substrate: ops-controller matches this checkout" in out


def test_doctor_passes_when_no_ops_controller_runs(monkeypatch, capsys):
    code, out = _doctor(monkeypatch, capsys, None)
    assert code == 0 and "not running" in out


def test_doctor_fails_when_the_digest_cannot_be_read(monkeypatch, capsys):
    code, out = _doctor(monkeypatch, capsys, doctor.SubstrateUnreadable("docker exec failed"))
    assert code == 1 and "docker exec failed" in out
