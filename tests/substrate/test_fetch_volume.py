"""`ordo fetch` into the models-gguf volume, and `ordo up` fetching what the render needs.

llama.cpp reads its weights from the `models-gguf` named volume (the 9p bind is retired), so a
download to a host directory is not a working model: it still had to be copied in by hand. The
fetch now runs a short-lived, digest-pinned helper container that mounts the volume, downloads
with resume, verifies the sha256 and moves the file into place atomically. Docker is never touched
here: the runner seam is replaced with a fake. The helper's shell script is exercised for real
with `sh` + `curl` against a file:// URL when those are on PATH.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ordo import bringup, cli, fetch
from ordo.catalog import Catalog, Model
from ordo.config import Source
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "catalog" / "models.yaml"
CATALOG = Catalog.load(CATALOG_PATH)

PROFILE_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128,
                "cpu_cores": 32, "platform": "Linux"}
PROFILE_CPU = {"gpus": [], "ram_gb": 16, "cpu_cores": 8, "platform": "Linux"}

HELLO = b"hello world"
HELLO_SHA = hashlib.sha256(HELLO).hexdigest()
TOKEN = "hf_this-is-a-test-token-value"


def _model(file="m.gguf", sha=HELLO_SHA, gated=False, source="https://example.test/m.gguf") -> Model:
    return Model.from_dict({"id": file.removesuffix(".gguf"), "file": file, "source": source,
                            "sha256": sha, "gated": gated, "requires": {"vram_gb": 1}})


class FakeRunner:
    """Stands in for the docker CLI. `volume_exists` and `files` describe the volume's state;
    `helper_exit` is what a helper run returns. Every call is recorded with the env it got."""

    def __init__(self, *, volume_exists=True, files=(), helper_exit=0):
        self.volume_exists = volume_exists
        self.files = set(files)
        self.helper_exit = helper_exit
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def run(self, argv, *, env=None, capture=False):
        self.calls.append((list(argv), env))
        if argv[:3] == ["docker", "volume", "inspect"]:
            return fetch.RunResult(0 if self.volume_exists else 1, "")
        if argv[:3] == ["docker", "volume", "create"]:
            self.volume_exists = True
            return fetch.RunResult(0, "")
        if fetch.LIST_MARKER in argv:
            return fetch.RunResult(0, "\n".join(sorted(self.files)) + "\n")
        return fetch.RunResult(self.helper_exit, "")

    def helper_runs(self):
        return [(argv, env) for argv, env in self.calls if fetch.FETCH_MARKER in argv]


def _write_out(tmp_path: Path, hardware=PROFILE_5090, secrets: str = "") -> Path:
    rc = render(Source.from_dict({"hardware": hardware, "model": "auto", "plugins": "auto"}), CATALOG)
    rc.write(tmp_path)
    (tmp_path / "secrets.env").write_text(secrets, encoding="utf-8")
    return tmp_path


def _doc(out: Path) -> dict:
    return yaml.safe_load((out / "docker-compose.yml").read_text(encoding="utf-8"))


def _env(out: Path) -> dict[str, str]:
    from ordo import parity
    return parity.load_env(str(out / ".env"))


# ── the helper container ─────────────────────────────────────────────────────


def test_helper_image_is_version_and_digest_pinned():
    assert re.fullmatch(r"[a-z0-9./-]+:\d+(\.\d+)+@sha256:[0-9a-f]{64}", fetch.HELPER_IMAGE)


def test_helper_argv_mounts_only_the_volume_and_runs_the_pinned_image():
    argv = fetch.helper_argv("ordo_models-gguf", _model())
    assert argv[:3] == ["docker", "run", "--rm"]
    assert fetch.HELPER_IMAGE in argv
    mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg in ("-v", "--volume", "--mount")]
    assert mounts == ["ordo_models-gguf:/models"]                       # no host path is mounted
    assert not any(":\\" in arg or arg.startswith(("/c/", "C:", "/home", "/Users")) for arg in argv)
    env = dict(argv[i + 1].split("=", 1) for i, arg in enumerate(argv) if arg == "-e" and "=" in argv[i + 1])
    assert env["ORDO_FETCH_FILE"] == "m.gguf"
    assert env["ORDO_FETCH_SHA256"] == HELLO_SHA
    assert env["ORDO_FETCH_URL"] == "https://example.test/m.gguf"


def test_a_gated_model_passes_the_token_by_name_only():
    argv = fetch.helper_argv("ordo_models-gguf", _model(gated=True))
    assert argv.count("HF_TOKEN") == 1
    assert ["-e", "HF_TOKEN"] == argv[argv.index("HF_TOKEN") - 1:argv.index("HF_TOKEN") + 1]
    assert not any(arg.startswith("HF_TOKEN=") for arg in argv)


def test_an_ungated_model_gets_no_token_at_all():
    assert "HF_TOKEN" not in fetch.helper_argv("ordo_models-gguf", _model())


@pytest.mark.parametrize("bad", ["../escape.gguf", "sub/dir.gguf", ".hidden.gguf", "", "a\\b.gguf"])
def test_a_file_name_that_could_leave_the_volume_is_refused(bad):
    with pytest.raises(ValueError, match="file name"):
        fetch.helper_argv("ordo_models-gguf", _model(file=bad))


# ── what the rendered stack needs from the volume ────────────────────────────


def test_the_whole_stack_needs_the_chat_cpu_fallback_and_embed_models(tmp_path):
    out = _write_out(tmp_path)
    doc, env = _doc(out), _env(out)
    needed = fetch.required_model_files(doc, env, list(doc["services"]))
    files = {n.file for n in needed if not n.optional}
    assert env["LLAMACPP_MODEL"] in files
    assert "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf" in files                   # llamacpp-cpu
    assert "nomic-embed-text-v1.5.Q4_K_M.gguf" in files                # llamacpp-embed


def test_a_service_that_reads_no_model_needs_nothing(tmp_path):
    out = _write_out(tmp_path)
    assert fetch.required_model_files(_doc(out), _env(out), ["dashboard", "ops-controller"]) == []


@pytest.mark.parametrize("hardware", [PROFILE_5090, PROFILE_CPU])
def test_every_file_a_default_render_needs_is_pinned_in_the_catalog(tmp_path, hardware):
    """The invariant that makes `ordo up` work with no manual model step: whatever the render
    reads from the volume has a catalog source and sha256 to fetch it from."""
    out = _write_out(tmp_path, hardware)
    doc = _doc(out)
    for need in fetch.required_model_files(doc, _env(out), list(doc["services"])):
        if need.optional:
            continue
        model = CATALOG.by_file(need.file)
        assert model is not None, f"{need.file} ({need.service}) has no catalog entry"
        assert model.sha256 and model.source.startswith("https://"), model.id


def test_support_models_are_never_picked_as_the_chat_model():
    support_ids = {m.id for m in CATALOG.support_models}
    assert support_ids                                                   # cpu fallback + embed
    assert not support_ids & {m.id for m in CATALOG.models}
    for hardware in (PROFILE_5090, PROFILE_CPU):
        rc = render(Source.from_dict({"hardware": hardware, "model": "auto", "plugins": "auto"}), CATALOG)
        assert rc.model.id not in support_ids
    with pytest.raises(ValueError):
        render(Source.from_dict({"hardware": PROFILE_CPU, "model": "nomic-embed-text-v1.5-q4",
                                 "plugins": "auto"}), CATALOG)


# ── ensure_models: `ordo up`'s fetch step ────────────────────────────────────


def _catalog(*models: Model) -> Catalog:
    return Catalog(list(models))


def _stack(file="m.gguf") -> tuple[dict, dict[str, str]]:
    doc = {"services": {"llamacpp": {"image": "x", "volumes": ["models-gguf:/models:ro"]},
                        "dashboard": {"image": "y"}}}
    return doc, {"LLAMACPP_MODEL": file, "LLAMACPP_MMPROJ": ""}


def test_up_fetches_nothing_when_every_file_is_present(capsys):
    doc, env = _stack()
    runner = FakeRunner(files={"m.gguf"})
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model()), project="ordo",
                               secrets={}, runner=runner, dry_run=False) == 0
    assert runner.helper_runs() == []                                    # idempotent: probe only


def test_up_fetches_a_missing_model_into_the_project_volume():
    doc, env = _stack()
    runner = FakeRunner(files=set())
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model()), project="ordo",
                               secrets={}, runner=runner, dry_run=False) == 0
    [(argv, _env_)] = runner.helper_runs()
    assert "ordo_models-gguf:/models" in argv
    assert "ORDO_FETCH_FILE=m.gguf" in argv


def test_up_skips_the_probe_entirely_when_nothing_starting_reads_a_model():
    doc, env = _stack()
    runner = FakeRunner()
    assert fetch.ensure_models(doc, env, ["dashboard"], catalog=_catalog(_model()), project="ordo",
                               secrets={}, runner=runner, dry_run=False) == 0
    assert runner.calls == []


def test_a_missing_volume_is_created_with_compose_labels_before_the_fetch():
    doc, env = _stack()
    runner = FakeRunner(volume_exists=False)
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model()), project="ordo",
                               secrets={}, runner=runner, dry_run=False) == 0
    create = next(argv for argv, _ in runner.calls if argv[:3] == ["docker", "volume", "create"])
    assert "com.docker.compose.project=ordo" in create
    assert "com.docker.compose.volume=models-gguf" in create
    assert create[-1] == "ordo_models-gguf"
    assert runner.calls.index((create, None)) < runner.calls.index(runner.helper_runs()[0])


def test_a_missing_file_with_no_catalog_entry_refuses(capsys):
    doc, env = _stack("hand-made.gguf")
    runner = FakeRunner()
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model()), project="ordo",
                               secrets={}, runner=runner, dry_run=False) == 1
    assert "hand-made.gguf" in capsys.readouterr().err
    assert runner.helper_runs() == []


def test_an_unpinned_model_is_refused_on_up(capsys):
    doc, env = _stack()
    runner = FakeRunner()
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model(sha=None)), project="ordo",
                               secrets={}, runner=runner, dry_run=False) == 1
    assert "sha256" in capsys.readouterr().err
    assert runner.helper_runs() == []


def test_a_missing_optional_projector_is_only_a_note(capsys):
    doc, env = _stack()
    env["LLAMACPP_MMPROJ"] = "/models/vision.gguf"
    runner = FakeRunner(files={"m.gguf"})
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model()), project="ordo",
                               secrets={}, runner=runner, dry_run=False) == 0
    assert "vision.gguf" in capsys.readouterr().out
    assert runner.helper_runs() == []


def test_a_gated_model_without_a_token_refuses_and_names_the_key(capsys):
    doc, env = _stack()
    runner = FakeRunner()
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model(gated=True)), project="ordo",
                               secrets={"HF_TOKEN": ""}, runner=runner, dry_run=False,
                               process_env={}) == 1
    assert "HF_TOKEN" in capsys.readouterr().err
    assert runner.helper_runs() == []


def test_a_gated_model_gets_the_token_through_the_environment_never_printed(capsys):
    doc, env = _stack()
    runner = FakeRunner()
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model(gated=True)), project="ordo",
                               secrets={"HF_TOKEN": TOKEN}, runner=runner, dry_run=False,
                               process_env={}) == 0
    [(argv, child_env)] = runner.helper_runs()
    assert child_env["HF_TOKEN"] == TOKEN
    assert TOKEN not in " ".join(argv)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_an_ungated_model_does_not_hand_the_helper_a_token():
    doc, env = _stack()
    runner = FakeRunner()
    fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model()), project="ordo",
                        secrets={"HF_TOKEN": TOKEN}, runner=runner, dry_run=False, process_env={})
    [(argv, child_env)] = runner.helper_runs()
    assert "HF_TOKEN" not in argv and "HF_TOKEN" not in (child_env or {})


def test_a_checksum_mismatch_fails_loud(capsys):
    doc, env = _stack()
    runner = FakeRunner(helper_exit=fetch.EXIT_CHECKSUM_MISMATCH)
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model()), project="ordo",
                               secrets={}, runner=runner, dry_run=False) == 1
    assert "checksum" in capsys.readouterr().err


def test_dry_run_prints_the_helper_and_downloads_nothing(capsys):
    doc, env = _stack()
    runner = FakeRunner(volume_exists=False)
    assert fetch.ensure_models(doc, env, ["llamacpp"], catalog=_catalog(_model()), project="ordo",
                               secrets={}, runner=runner, dry_run=True) == 0
    assert runner.helper_runs() == []
    assert not any(argv[:3] == ["docker", "volume", "create"] for argv, _ in runner.calls)
    assert "m.gguf" in capsys.readouterr().out


# ── bring_up wiring ──────────────────────────────────────────────────────────

IDLE = {"state": "idle", "leased": False, "running": [], "queued": [], "evicted_residents": {}}


@pytest.fixture
def no_docker(monkeypatch):
    monkeypatch.setattr("ordo.bringup.read_gpu_status", lambda project: IDLE)
    monkeypatch.setattr("ordo.images.ensure_images", lambda *a, **k: 0)
    ran: list[list[str]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr("ordo.bringup.subprocess.run", lambda cmd, *a, **kw: ran.append(list(cmd)) or Result())
    return ran


def _capture_fetch(monkeypatch, code=0) -> list[set[str]]:
    seen: list[set[str]] = []

    def fake(out_dir, doc, services, *, project, catalog_path, dry_run):
        seen.append(set(services))
        return code

    monkeypatch.setattr("ordo.fetch.ensure_models_for_render", fake)
    return seen


def test_up_fetches_for_exactly_the_services_it_starts(monkeypatch, tmp_path, no_docker):
    out = _write_out(tmp_path)
    seen = _capture_fetch(monkeypatch)
    assert bringup.bring_up(str(out), "ordo", ["llamacpp"], whole_stack=False, with_profiles=True,
                            force_recreate=False, dry_run=False, models_catalog=CATALOG_PATH) == 0
    assert seen == [{"llamacpp"}]
    assert no_docker                                                     # compose ran after


def test_up_without_a_catalog_does_not_fetch(monkeypatch, tmp_path, no_docker):
    out = _write_out(tmp_path)
    seen = _capture_fetch(monkeypatch)
    bringup.bring_up(str(out), "ordo", [], whole_stack=True, with_profiles=True,
                     force_recreate=False, dry_run=False, models_catalog=None)
    assert seen == []


def test_a_failed_fetch_stops_the_bring_up(monkeypatch, tmp_path, no_docker):
    out = _write_out(tmp_path)
    _capture_fetch(monkeypatch, code=1)
    assert bringup.bring_up(str(out), "ordo", [], whole_stack=True, with_profiles=True,
                            force_recreate=False, dry_run=False, models_catalog=CATALOG_PATH) == 1
    assert no_docker == []                                               # compose never ran


def test_the_fetch_runs_after_the_image_build(monkeypatch, tmp_path, no_docker):
    out = _write_out(tmp_path)
    order: list[str] = []
    monkeypatch.setattr("ordo.images.ensure_images", lambda *a, **k: order.append("build") or 0)
    monkeypatch.setattr("ordo.fetch.ensure_models_for_render", lambda *a, **k: order.append("fetch") or 0)
    bringup.bring_up(str(out), "ordo", [], whole_stack=True, with_profiles=True, force_recreate=False,
                     dry_run=False, build=True, models_catalog=CATALOG_PATH)
    assert order == ["build", "fetch"]


def test_cli_up_fetches_by_default_and_no_fetch_turns_it_off(monkeypatch, tmp_path, no_docker):
    monkeypatch.setattr(cli, "_host_preflight", lambda *a, **k: True)
    out = str(_write_out(tmp_path))
    seen = _capture_fetch(monkeypatch)
    assert cli.main(["up", "--all", "--out", out, "--no-build"]) == 0
    assert cli.main(["up", "--all", "--out", out, "--no-build", "--no-fetch"]) == 0
    assert len(seen) == 1


# ── the helper's shell script, run for real ──────────────────────────────────

_SH = shutil.which("sh")
_TOOLS = _SH and shutil.which("curl") and shutil.which("sha256sum")


def _run_script(models_dir: Path, url: str, file: str, sha: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, ORDO_FETCH_DIR=str(models_dir), ORDO_FETCH_URL=url,
               ORDO_FETCH_FILE=file, ORDO_FETCH_SHA256=sha)
    env.pop("HF_TOKEN", None)
    return subprocess.run([_SH, "-c", fetch.HELPER_SCRIPT], env=env, capture_output=True, text=True,
                          timeout=60)


@pytest.fixture
def served(tmp_path) -> tuple[Path, str]:
    src = tmp_path / "src" / "m.gguf"
    src.parent.mkdir()
    src.write_bytes(HELLO)
    dest = tmp_path / "volume"
    dest.mkdir()
    return dest, src.as_uri()


@pytest.mark.skipif(not _TOOLS, reason="needs sh, curl and sha256sum on PATH")
def test_script_downloads_verifies_and_moves_into_place(served):
    dest, url = served
    proc = _run_script(dest, url, "m.gguf", HELLO_SHA)
    assert proc.returncode == 0, proc.stderr
    assert (dest / "m.gguf").read_bytes() == HELLO
    assert sorted(p.name for p in dest.iterdir()) == ["m.gguf"]          # no partial left behind


@pytest.mark.skipif(not _TOOLS, reason="needs sh, curl and sha256sum on PATH")
def test_script_is_idempotent_once_the_file_is_verified(served):
    dest, url = served
    assert _run_script(dest, url, "m.gguf", HELLO_SHA).returncode == 0
    again = _run_script(dest, "file:///nonexistent/never-fetched.gguf", "m.gguf", HELLO_SHA)
    assert again.returncode == 0, again.stderr                         # no download attempted


@pytest.mark.skipif(not _TOOLS, reason="needs sh, curl and sha256sum on PATH")
def test_script_sha_mismatch_fails_loud_and_leaves_no_file(served):
    dest, url = served
    proc = _run_script(dest, url, "m.gguf", "0" * 64)
    assert proc.returncode == fetch.EXIT_CHECKSUM_MISMATCH
    assert "checksum" in proc.stderr
    assert list(dest.iterdir()) == []                                    # neither the file nor a .part


@pytest.mark.skipif(not _TOOLS, reason="needs sh, curl and sha256sum on PATH")
def test_script_resumes_a_partial_download(served):
    dest, url = served
    (dest / ".m.gguf.part").write_bytes(HELLO[:5])
    proc = _run_script(dest, url, "m.gguf", HELLO_SHA)
    assert proc.returncode == 0, proc.stderr
    assert (dest / "m.gguf").read_bytes() == HELLO
    assert not (dest / ".m.gguf.part").exists()


@pytest.mark.skipif(not _TOOLS, reason="needs sh, curl and sha256sum on PATH")
def test_script_replaces_a_corrupt_file_only_after_the_new_one_verifies(served):
    dest, url = served
    (dest / "m.gguf").write_bytes(b"corrupt")
    proc = _run_script(dest, url, "m.gguf", HELLO_SHA)
    assert proc.returncode == 0, proc.stderr
    assert (dest / "m.gguf").read_bytes() == HELLO
