"""A catalog model that is built locally from a tracked recipe instead of downloaded.

Such an entry pins `sha256` and `size_bytes` and names a conversion recipe in services/ninfer/convert
(`build:`) instead of a `source:` URL. Nothing can download it, so `ordo fetch`, `ordo up` and
`ordo apply` only verify the file in the models volume against the pin. A missing or mismatched file
refuses the bring-up with the exact rebuild and install commands; it is never downloaded from
somewhere else and never served unverified. `ordo fetch <id> --from <file>` installs a locally built
file through the same verifying helper as a download."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ordo.host import fetch
from ordo.render import models_volume
from ordo.render.catalog import Catalog, Model

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
CONVERT = ROOT / "services" / "ninfer" / "convert"
HERETIC = "qwen3.8-27b-heretic-ara-ninfer"

DATA = b"converted weights"
DATA_SHA = hashlib.sha256(DATA).hexdigest()


def _entry(**extra) -> dict:
    base = {"id": "b", "file": "b.ninfer", "build": "qwen3.8-27b-heretic-ara", "sha256": DATA_SHA,
            "size_bytes": len(DATA), "requires": {"vram_gb": 22}, "backend": "ninfer",
            "backend_image": "ordo/ninfer"}
    base.update(extra)
    return {k: v for k, v in base.items() if v is not None}


def _built() -> Model:
    return Model.from_dict(_entry())


class FakeRunner:
    def __init__(self, *, files=(), helper_exit=0):
        self.files = set(files)
        self.helper_exit = helper_exit
        self.calls: list[list[str]] = []

    def run(self, argv, *, env=None, capture=False):
        self.calls.append(list(argv))
        if argv[:3] == ["docker", "volume", "inspect"]:
            return models_volume.RunResult(0, "")
        if models_volume.LIST_MARKER in argv:
            return models_volume.RunResult(0, "\n".join(sorted(self.files)) + "\n")
        return models_volume.RunResult(self.helper_exit, "")

    def helper_runs(self):
        return [argv for argv in self.calls if fetch.FETCH_MARKER in argv]


def _stack(file="b.ninfer"):
    doc = {"services": {"llamacpp": {"image": "x", "volumes": ["models-gguf:/models:ro"]}}}
    return doc, {"LLAMACPP_MODEL": file, "LLAMACPP_MMPROJ": ""}


# --- the catalog contract ------------------------------------------------------------------------

def test_a_built_entry_has_a_recipe_and_no_source():
    model = _built()
    assert model.build == "qwen3.8-27b-heretic-ara"
    assert model.source == ""


def test_a_built_entry_cannot_also_name_a_source():
    with pytest.raises(ValueError, match="build"):
        Model.from_dict(_entry(source="https://example.invalid/b.ninfer"))


@pytest.mark.parametrize("missing", ["sha256", "size_bytes"])
def test_a_built_entry_must_pin_its_bytes(missing):
    with pytest.raises(ValueError, match=missing):
        Model.from_dict(_entry(**{missing: None}))


def test_a_recipe_name_cannot_leave_the_recipe_directory():
    with pytest.raises(ValueError, match="build"):
        Model.from_dict(_entry(build="../../etc/passwd"))


def test_every_built_catalog_entry_has_a_complete_pinned_recipe():
    built = [m for m in CATALOG.models if m.build]
    assert [m.id for m in built] == [HERETIC]
    for model in built:
        sources = CONVERT / "inputs" / f"{model.build}.sources"
        args = CONVERT / "inputs" / f"{model.build}.args"
        assert sources.is_file() and args.is_file(), model.id
        lines = [line.split() for line in sources.read_text().splitlines()
                 if line.strip() and not line.startswith("#")]
        assert lines, model.id
        for sha, path, url in lines:
            assert re.fullmatch(r"[0-9a-f]{64}", sha), path
            # pinned by revision: a full commit sha in the URL, never a branch
            assert url.startswith("https://") and re.search(r"/resolve/[0-9a-f]{40}/", url), url
        argv = [line for line in args.read_text().splitlines() if line and not line.startswith("#")]
        assert Path(argv[argv.index("--out") + 1]).name == model.file
        assert argv[argv.index("--device") + 1] == "cpu"          # never the GPU


def test_the_converter_runs_the_engine_commit_that_serves_the_artifact():
    def commit(dockerfile: Path) -> str:
        return re.search(r"^ARG NINFER_COMMIT=([0-9a-f]{40})$", dockerfile.read_text(), re.M).group(1)
    assert commit(CONVERT / "Dockerfile") == commit(ROOT / "services" / "ninfer" / "Dockerfile")


# --- ordo fetch / up / apply: verify only, refuse with the rebuild commands ------------------------

def test_a_built_model_is_never_refused_for_its_missing_url():
    assert fetch.refusal(_built()) is None


def test_the_helper_gets_no_url_for_a_built_model():
    argv = fetch.helper_argv("ordo_models-gguf", _built())
    assert "ORDO_FETCH_URL=" in argv
    assert not any(a.startswith("ORDO_FETCH_FROM=") for a in argv)


def test_installing_a_local_file_mounts_only_its_directory_read_only(tmp_path):
    local = tmp_path / "out" / "b.ninfer"
    argv = fetch.helper_argv("ordo_models-gguf", _built(), local_file=local)
    assert f"{local.parent.resolve().as_posix()}:{fetch.LOCAL_MOUNT}:ro" in argv
    assert f"ORDO_FETCH_FROM={fetch.LOCAL_MOUNT}/b.ninfer" in argv


def test_up_verifies_a_built_model_even_when_it_is_present():
    doc, env = _stack()
    runner = FakeRunner(files={"b.ninfer"})
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=Catalog([_built()]), project="ordo",
                               secrets={}, runner=runner, dry_run=False) == 0
    [argv] = runner.helper_runs()
    assert f"ORDO_FETCH_SHA256={DATA_SHA}" in argv


@pytest.mark.parametrize("files", [set(), {"b.ninfer"}])
def test_up_refuses_a_missing_or_wrong_built_model_with_the_rebuild_commands(files, capsys):
    doc, env = _stack()
    runner = FakeRunner(files=files, helper_exit=fetch.EXIT_NOT_BUILT)
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=Catalog([_built()]), project="ordo",
                               secrets={}, runner=runner, dry_run=False) == 1
    err = capsys.readouterr().err
    assert "services/ninfer/convert/run.sh qwen3.8-27b-heretic-ara" in err
    assert "ordo fetch b --from" in err
    assert "sha256" in err


def test_dry_run_says_it_would_verify_a_built_model(capsys):
    doc, env = _stack()
    runner = FakeRunner(files={"b.ninfer"})
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=Catalog([_built()]), project="ordo",
                               secrets={}, runner=runner, dry_run=True) == 0
    assert runner.helper_runs() == []
    assert "would verify b (b.ninfer)" in capsys.readouterr().out


# --- the helper's shell script, run for real ---------------------------------------------------------

_SH = shutil.which("sh")
_TOOLS = _SH and shutil.which("sha256sum")


def _run(volume: Path, sha: str, local: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, ORDO_FETCH_DIR=str(volume), ORDO_FETCH_URL="", ORDO_FETCH_FILE="b.ninfer",
               ORDO_FETCH_SHA256=sha, ORDO_FETCH_FROM=str(local) if local else "")
    return subprocess.run([_SH, "-c", fetch.HELPER_SCRIPT], env=env, capture_output=True, text=True,
                          timeout=60)


@pytest.fixture
def volume(tmp_path) -> Path:
    path = tmp_path / "volume"
    path.mkdir()
    return path


@pytest.mark.skipif(not _TOOLS, reason="needs sh and sha256sum on PATH")
def test_script_refuses_a_missing_built_file(volume):
    proc = _run(volume, DATA_SHA)
    assert proc.returncode == fetch.EXIT_NOT_BUILT
    assert list(volume.iterdir()) == []


@pytest.mark.skipif(not _TOOLS, reason="needs sh and sha256sum on PATH")
def test_script_accepts_a_present_verified_built_file(volume):
    (volume / "b.ninfer").write_bytes(DATA)
    assert _run(volume, DATA_SHA).returncode == 0


@pytest.mark.skipif(not _TOOLS, reason="needs sh and sha256sum on PATH")
def test_script_refuses_a_mismatched_built_file_and_never_replaces_it(volume):
    (volume / "b.ninfer").write_bytes(b"another conversion")
    proc = _run(volume, DATA_SHA)
    assert proc.returncode == fetch.EXIT_NOT_BUILT
    assert (volume / "b.ninfer").read_bytes() == b"another conversion"


@pytest.mark.skipif(not _TOOLS, reason="needs sh and sha256sum on PATH")
def test_script_installs_a_local_file_only_when_it_verifies(volume, tmp_path):
    good = tmp_path / "good.ninfer"
    good.write_bytes(DATA)
    assert _run(volume, DATA_SHA, good).returncode == 0
    assert (volume / "b.ninfer").read_bytes() == DATA
    assert sorted(p.name for p in volume.iterdir()) == ["b.ninfer"]


@pytest.mark.skipif(not _TOOLS, reason="needs sh and sha256sum on PATH")
def test_script_rejects_a_local_file_that_does_not_match(volume, tmp_path):
    bad = tmp_path / "bad.ninfer"
    bad.write_bytes(b"not the pinned bytes")
    proc = _run(volume, DATA_SHA, bad)
    assert proc.returncode == fetch.EXIT_CHECKSUM_MISMATCH
    assert list(volume.iterdir()) == []


# --- ordo fetch --from ---------------------------------------------------------------------------------

def _fetch_args(model, local):
    import argparse
    return argparse.Namespace(model=model, local_file=str(local))


def test_the_cli_passes_from_to_the_fetch(monkeypatch, tmp_path):
    from ordo import cli
    from ordo.host import cli_stack
    seen = []
    monkeypatch.setattr(cli_stack, "cmd_fetch", lambda args: seen.append(args) or 0)
    assert cli.main(["fetch", HERETIC, "--from", str(tmp_path / "x.ninfer")]) == 0
    assert seen[0].model == HERETIC and seen[0].local_file == str(tmp_path / "x.ninfer")


def test_fetch_from_installs_only_a_named_built_model(tmp_path, capsys):
    from ordo.host import cli_stack
    local = tmp_path / "qwen3_8_27b_heretic_ara.ninfer"
    local.write_bytes(DATA)
    assert cli_stack.local_install_target(_fetch_args(HERETIC, local), CATALOG).id == HERETIC
    assert cli_stack.local_install_target(_fetch_args(None, local), CATALOG) is None
    assert "name the catalog id" in capsys.readouterr().err
    downloadable = next(m.id for m in CATALOG.models if m.source)
    assert cli_stack.local_install_target(_fetch_args(downloadable, local), CATALOG) is None
    assert "downloads its file" in capsys.readouterr().err
    assert cli_stack.local_install_target(_fetch_args(HERETIC, tmp_path / "absent"), CATALOG) is None
    assert "no such file" in capsys.readouterr().err
