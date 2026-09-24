"""First-party image identity: `ordo build` tags, the out/images.json record, and render pinning.

17 first-party services used to run `ordo/<name>:latest`, so nothing said which commit a container
ran and a rollback was a hand retag. Now `ordo build` tags each image with the commit that last
changed its build inputs, records the tag in out/images.json, and every render (the host's and
ops-controller's, which both write into the same out/ directory) pins the compose to that record.
Docker is never touched here: the Docker seam is replaced with a fake.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from ordo import bringup, buildspec, images
from ordo.agents import AgentRegistry
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.dashboards import DashboardRegistry
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
PLUGINS = PluginRegistry.load(SERVICES)
AGENTS = AgentRegistry.load(SERVICES)
DASHBOARDS = DashboardRegistry.load(SERVICES)
FIRST_PARTY = images.first_party_contexts(PLUGINS, AGENTS, DASHBOARDS, project="ordo")

PROFILE_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128,
                "cpu_cores": 32, "platform": "Linux"}


def _rc(hardware=PROFILE_5090):
    return render(Source.from_dict({"hardware": hardware, "model": "auto", "plugins": "auto"}), CATALOG)


# A 5090 that reports its compute capability: the sizer picks a model that needs the patched sm_120 build.
PROFILE_5090_SM120 = {**PROFILE_5090, "gpus": [{"name": "RTX 5090", "vram_gb": 32, "compute_cap": "12.0"}]}


class FakeDocker:
    """Records builds and tags; `present` is the set of image refs the local daemon holds."""

    def __init__(self, present: set[str] | None = None, fail: set[str] | None = None):
        self.present = set(present or ())
        self.fail = set(fail or ())
        self.builds: list[tuple[str, str, str, dict[str, str]]] = []
        self.tags: list[tuple[str, str]] = []

    def image_exists(self, ref: str) -> bool:
        return ref in self.present

    def build(self, target: images.BuildTarget, ref: str, labels: dict[str, str]) -> bool:
        self.builds.append((ref, target.dockerfile, target.context, dict(labels)))
        if target.image in self.fail:
            return False
        self.present.add(ref)
        return True

    def tag(self, source: str, target: str) -> bool:
        self.tags.append((source, target))
        self.present.add(target)
        return True


class FakeGit:
    def __init__(self, commits: dict[tuple[str, ...], str], dirty: set[tuple[str, ...]] = frozenset(),
                 head: str = "f" * 40):
        self.commits = commits
        self.dirty = set(dirty)
        self._head = head

    def last_commit(self, paths):
        return self.commits.get(tuple(paths), "")

    def head(self):
        return self._head

    def is_dirty(self, paths):
        return tuple(paths) in self.dirty


# --- the record ---


def test_missing_record_is_empty(tmp_path):
    assert images.load_record(tmp_path) == {}


def test_record_round_trips_and_merges(tmp_path):
    images.save_record(tmp_path, {"ordo/dashboard": "aaaaaaaaaaaa"})
    images.save_record(tmp_path, {**images.load_record(tmp_path), "ordo/gpu-gate": "bbbbbbbbbbbb"})
    assert images.load_record(tmp_path) == {"ordo/dashboard": "aaaaaaaaaaaa", "ordo/gpu-gate": "bbbbbbbbbbbb"}
    doc = json.loads((tmp_path / images.RECORD_FILE).read_text(encoding="utf-8"))
    assert doc["images"]["ordo/dashboard"] == "aaaaaaaaaaaa"


@pytest.mark.parametrize("text", ["{not json", '{"images": ["x"]}', '{"images": {"ordo/x": "bad tag!"}}'])
def test_malformed_record_is_an_error_not_a_silent_fallback(tmp_path, text):
    (tmp_path / images.RECORD_FILE).write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="images.json"):
        images.load_record(tmp_path)


# --- tag derivation ---


def test_tag_is_the_short_commit_that_last_changed_the_inputs():
    git = FakeGit({("services/gpu-gate",): "0123456789abcdef" * 2 + "01234567"})
    assert images.content_tag(git, ("services/gpu-gate",)) == ("0123456789ab", False)


def test_uncommitted_changes_to_the_inputs_add_dirty():
    git = FakeGit({("services/gpu-gate",): "0123456789ab" + "0" * 28}, dirty={("services/gpu-gate",)})
    assert images.content_tag(git, ("services/gpu-gate",)) == ("0123456789ab-dirty", True)


def test_never_committed_inputs_fall_back_to_head_and_are_dirty():
    git = FakeGit({}, dirty={("services/new",)}, head="abcdefabcdef" + "0" * 28)
    assert images.content_tag(git, ("services/new",)) == ("abcdefabcdef-dirty", True)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_real_git_tags_follow_only_their_own_context(tmp_path):
    """Against a real repository: a commit to one context moves only that context's tag."""
    repo = tmp_path / "repo"
    (repo / "services" / "a").mkdir(parents=True)
    (repo / "services" / "b").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(repo, "config", key, value)
    (repo / "services" / "a" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repo / "services" / "b" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "services" / "b" / "Dockerfile").write_text("FROM scratch\nLABEL x=y\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "two")
    second = _git(repo, "rev-parse", "HEAD")

    git = images.Git(repo)
    assert images.content_tag(git, ("services/a",)) == (first[:12], False)
    assert images.content_tag(git, ("services/b",)) == (second[:12], False)
    (repo / "services" / "a" / "extra.txt").write_text("untracked\n", encoding="utf-8")
    assert images.content_tag(git, ("services/a",)) == (first[:12] + "-dirty", True)
    assert images.content_tag(git, ("services/b",)) == (second[:12], False)


def test_root_context_image_is_identified_by_the_dockerignore_allowlist():
    """ops-controller builds from the repo root; its identity is exactly what .dockerignore lets in."""
    target = images.build_target("ordo/ops-controller", "services/ops-controller", ROOT)
    assert target.context == "."
    assert target.dockerfile == "services/ops-controller/Dockerfile"
    assert set(target.inputs) == {"ordo", "catalog", "services"}
    plain = images.build_target("ordo/dashboard", "services/dashboard/dashboard", ROOT)
    assert (plain.context, plain.dockerfile, plain.inputs) == (
        "services/dashboard/dashboard", "services/dashboard/dashboard/Dockerfile",
        ("services/dashboard/dashboard",))


# --- building ---

GATE = images.BuildTarget("ordo/gpu-gate", "services/gpu-gate/Dockerfile", "services/gpu-gate",
                          ("services/gpu-gate",))
SHA = "0123456789ab" + "c" * 28


def test_build_tags_the_commit_moves_current_and_records(tmp_path):
    docker = FakeDocker()
    code = images.build_images([GATE], git=FakeGit({GATE.inputs: SHA}), docker=docker, out_dir=tmp_path)
    assert code == 0
    ref = "ordo/gpu-gate:0123456789ab"
    assert docker.builds == [(ref, GATE.dockerfile, GATE.context,
                              {images.REVISION_LABEL: SHA})]
    assert docker.tags == [(ref, "ordo/gpu-gate:current")]
    assert images.load_record(tmp_path) == {"ordo/gpu-gate": "0123456789ab"}


def test_build_is_idempotent_when_the_content_tag_exists(tmp_path):
    docker = FakeDocker(present={"ordo/gpu-gate:0123456789ab"})
    code = images.build_images([GATE], git=FakeGit({GATE.inputs: SHA}), docker=docker, out_dir=tmp_path)
    assert code == 0
    assert docker.builds == []                                   # nothing rebuilt
    assert docker.tags == [("ordo/gpu-gate:0123456789ab", "ordo/gpu-gate:current")]
    assert images.load_record(tmp_path) == {"ordo/gpu-gate": "0123456789ab"}


def test_a_dirty_tag_always_rebuilds(tmp_path):
    """`<sha>-dirty` names no single content, so an existing one is never trusted."""
    docker = FakeDocker(present={"ordo/gpu-gate:0123456789ab-dirty"})
    git = FakeGit({GATE.inputs: SHA}, dirty={GATE.inputs})
    assert images.build_images([GATE], git=git, docker=docker, out_dir=tmp_path) == 0
    assert [b[0] for b in docker.builds] == ["ordo/gpu-gate:0123456789ab-dirty"]


def test_a_failed_build_records_nothing_for_it_and_fails(tmp_path):
    other = images.BuildTarget("ordo/dashboard", "services/dashboard/dashboard/Dockerfile",
                               "services/dashboard/dashboard", ("services/dashboard/dashboard",))
    docker = FakeDocker(fail={"ordo/gpu-gate"})
    git = FakeGit({GATE.inputs: SHA, other.inputs: "a" * 40})
    assert images.build_images([GATE, other], git=git, docker=docker, out_dir=tmp_path) == 1
    assert images.load_record(tmp_path) == {"ordo/dashboard": "a" * 12}   # the good one still lands
    assert ("ordo/gpu-gate:0123456789ab", "ordo/gpu-gate:current") not in docker.tags


def test_dry_run_builds_and_records_nothing(tmp_path):
    docker = FakeDocker()
    assert images.build_images([GATE], git=FakeGit({GATE.inputs: SHA}), docker=docker,
                               out_dir=tmp_path, dry_run=True) == 0
    assert docker.builds == [] and docker.tags == []
    assert not (tmp_path / images.RECORD_FILE).exists()


# --- which images are first-party ---


def test_first_party_set_covers_the_substrate_and_manifest_builds():
    for ident in ("ordo/ops-controller", "ordo/model-gateway", "ordo/gpu-gate", "ordo/dashboard",
                  "ordo/agent-hermes", "ordo/rag-ingestion", "ordo/n8n-mcp", "ordo/llamacpp-patched"):
        assert ident in FIRST_PARTY, ident


def test_pinned_and_external_images_are_not_first_party():
    # ltx-trainer carries its own pinned tag; openai-agent is built out of band. `ordo build` must not
    # retag either.
    assert "ordo/ltx-trainer" not in FIRST_PARTY
    assert "ordo/agent-openai-agent" not in FIRST_PARTY


def test_no_manifest_declares_a_first_party_image_on_a_rolling_tag():
    """Render owns the tag. A `:latest` in a manifest would bypass it and float again."""
    resolve = buildspec.context_resolver(PLUGINS, AGENTS, DASHBOARDS, project="ordo")
    declared = [str(i) for p in PLUGINS.plugins for i in buildspec._plugin_images(p)]
    declared += [a.image_for("ordo") for a in AGENTS.agents] + [d.image_for("ordo") for d in DASHBOARDS.dashboards]
    rolling = [ref for ref in declared
               if ref.endswith(":latest") and resolve(ref) not in (None, buildspec.EXTERNAL)]
    assert not rolling, rolling


# --- render pins the recorded tag ---


def _compose_images(out: Path) -> dict[str, str]:
    doc = yaml.safe_load((out / "docker-compose.yml").read_text(encoding="utf-8"))
    return {name: svc["image"] for name, svc in doc["services"].items()}


def test_render_without_a_record_uses_current_and_never_latest(tmp_path):
    _rc().write(tmp_path)
    refs = _compose_images(tmp_path)
    assert refs["ops-controller"] == "ordo/ops-controller:current"
    assert refs["dashboard"] == "ordo/dashboard:current"
    assert refs["agent"] == "ordo/agent-hermes:current"
    assert not [r for r in refs.values() if r.endswith(":latest") and r.startswith("ordo/")]


def test_render_pins_the_recorded_tag_and_leaves_the_rest_alone(tmp_path):
    images.save_record(tmp_path, {"ordo/ops-controller": "0123456789ab", "ordo/agent-hermes": "a" * 12})
    rc = _rc()
    rc.write(tmp_path)
    refs = _compose_images(tmp_path)
    assert refs["ops-controller"] == "ordo/ops-controller:0123456789ab"
    assert refs["agent"] == "ordo/agent-hermes:" + "a" * 12
    assert refs["dashboard"] == "ordo/dashboard:current"            # not built yet
    # upstream and pinned images are untouched by the record
    assert refs["litellm-db"] == rc.compose_dict()["services"]["litellm-db"]["image"]
    assert "@sha256:" in refs["llamacpp"] or refs["llamacpp"] == "ordo/llamacpp-patched:current"


def test_every_rendered_first_party_ref_is_current_or_recorded(tmp_path):
    record = {ident: "b" * 12 for ident in FIRST_PARTY}
    images.save_record(tmp_path, record)
    _rc().write(tmp_path)
    for name, ref in _compose_images(tmp_path).items():
        if buildspec.image_ident(ref) in FIRST_PARTY:
            assert ref == f"{buildspec.image_ident(ref)}:{'b' * 12}", (name, ref)


def test_ops_controller_render_reads_the_same_record(tmp_path):
    """A model switch inside ops-controller re-renders into /config (= out/) and must keep the pins."""
    from ordo.broker import Broker, MockBackend
    from ordo.control import ControlPlane
    from ordo.scheduler import Scheduler

    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump({"hardware": PROFILE_5090, "model": "auto", "plugins": "auto"}))
    out = tmp_path / "out"
    images.save_record(out, {"ordo/dashboard": "c" * 12})
    cp = ControlPlane(src, CATALOG, PLUGINS, out, scheduler=Scheduler(32), broker=Broker(Scheduler(32), MockBackend()))
    code, body = cp.route("POST", "/model-config", {"model": CATALOG.models[0].id})
    assert code == 200, body
    assert _compose_images(out)["dashboard"] == "ordo/dashboard:" + "c" * 12


# --- `ordo build` target selection ---

COMPOSE = {"services": {
    "ops-controller": {"image": "ordo/ops-controller:current"},
    "dashboard": {"image": "ordo/dashboard:0123456789ab"},
    "model-gateway": {"image": "ordo/model-gateway:current"},
    "model-gateway-keys": {"image": "ordo/model-gateway:current"},
    "litellm-db": {"image": "postgres:16-alpine@sha256:" + "1" * 64},
    "ltx-trainer": {"image": "${LTX_TRAINER_IMAGE:-ordo/ltx-trainer:9377758}", "profiles": ["ltx"]},
    "rag-ingestion": {"image": "ordo/rag-ingestion:current", "profiles": ["rag"]},
}}


def test_all_selects_each_first_party_image_the_compose_references_once():
    assert images.select_images(COMPOSE, FIRST_PARTY, None) == [
        "ordo/dashboard", "ordo/model-gateway", "ordo/ops-controller", "ordo/rag-ingestion"]


def test_named_services_select_their_images():
    assert images.select_images(COMPOSE, FIRST_PARTY, ["model-gateway-keys", "dashboard"]) == [
        "ordo/dashboard", "ordo/model-gateway"]


@pytest.mark.parametrize("service", ["litellm-db", "ltx-trainer", "nope"])
def test_naming_a_service_that_is_not_first_party_is_refused(service):
    with pytest.raises(ValueError, match=service):
        images.select_images(COMPOSE, FIRST_PARTY, [service])


# --- `ordo up` builds only what is missing ---


def _out(tmp_path, compose=COMPOSE) -> Path:
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose), encoding="utf-8")
    return tmp_path


def test_ensure_built_builds_only_the_missing_images(tmp_path):
    docker = FakeDocker(present={"ordo/ops-controller:current", "ordo/dashboard:0123456789ab"})
    built: list[list[str]] = []

    def fake_build(targets, **kw):
        built.append([t.image for t in targets])
        for t in targets:
            docker.present.add(f"{t.image}:current")
        return 0

    code = images.ensure_built(COMPOSE, {"ops-controller", "dashboard", "model-gateway", "litellm-db"},
                               first_party=FIRST_PARTY, repo_root=ROOT, out_dir=tmp_path,
                               docker=docker, git=FakeGit({}), build=fake_build)
    assert code == 0
    assert built == [["ordo/model-gateway"]]


def test_ensure_built_does_nothing_when_everything_is_present(tmp_path):
    docker = FakeDocker(present={"ordo/ops-controller:current", "ordo/dashboard:0123456789ab"})

    def fail_build(targets, **kw):
        raise AssertionError("nothing is missing")

    assert images.ensure_built(COMPOSE, {"ops-controller", "dashboard"}, first_party=FIRST_PARTY,
                               repo_root=ROOT, out_dir=tmp_path, docker=docker, git=FakeGit({}),
                               build=fail_build) == 0


def test_ensure_built_refuses_when_the_build_cannot_satisfy_a_stale_pin(tmp_path, capsys):
    """The compose names a tag this checkout no longer produces: say re-render, do not start."""
    docker = FakeDocker()

    def fake_build(targets, **kw):
        for t in targets:
            docker.present.add(f"{t.image}:ffffffffffff")
        return 0

    code = images.ensure_built(COMPOSE, {"dashboard"}, first_party=FIRST_PARTY, repo_root=ROOT,
                               out_dir=tmp_path, docker=docker, git=FakeGit({}), build=fake_build)
    assert code == 1
    assert "ordo/dashboard:0123456789ab" in capsys.readouterr().err


IDLE = {"state": "idle", "leased": False, "running": [], "queued": [], "evicted_residents": {}}


@pytest.fixture
def no_docker(monkeypatch):
    monkeypatch.setattr("ordo.bringup.read_gpu_status", lambda project: IDLE)
    ran: list[list[str]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr("ordo.bringup.subprocess.run", lambda cmd, *a, **kw: ran.append(list(cmd)) or Result())
    return ran


def _capture_ensure(monkeypatch) -> list[set[str]]:
    seen: list[set[str]] = []

    def fake(out_dir, doc, services, *, project, dry_run):
        seen.append(set(services))
        return 0

    monkeypatch.setattr("ordo.images.ensure_images", fake)
    return seen


def test_up_all_checks_every_service(monkeypatch, tmp_path, no_docker):
    seen = _capture_ensure(monkeypatch)
    assert bringup.bring_up(str(_out(tmp_path)), "ordo", [], whole_stack=True, with_profiles=True,
                            force_recreate=False, dry_run=False, build=True) == 0
    assert seen == [set(COMPOSE["services"])]


def test_up_core_skips_profiled_services(monkeypatch, tmp_path, no_docker):
    seen = _capture_ensure(monkeypatch)
    bringup.bring_up(str(_out(tmp_path)), "ordo", [], whole_stack=True, with_profiles=False,
                     force_recreate=False, dry_run=False, build=True)
    assert seen == [{"ops-controller", "dashboard", "model-gateway", "model-gateway-keys", "litellm-db"}]


def test_up_named_checks_only_the_named_services(monkeypatch, tmp_path, no_docker):
    seen = _capture_ensure(monkeypatch)
    bringup.bring_up(str(_out(tmp_path)), "ordo", ["dashboard"], whole_stack=False, with_profiles=True,
                     force_recreate=False, dry_run=False, build=True)
    assert seen == [{"dashboard"}]


def test_no_build_skips_the_check(monkeypatch, tmp_path, no_docker):
    seen = _capture_ensure(monkeypatch)
    bringup.bring_up(str(_out(tmp_path)), "ordo", [], whole_stack=True, with_profiles=True,
                     force_recreate=False, dry_run=False, build=False)
    assert seen == []


def test_a_failed_build_stops_the_bring_up(monkeypatch, tmp_path, no_docker):
    monkeypatch.setattr("ordo.images.ensure_images", lambda *a, **kw: 1)
    assert bringup.bring_up(str(_out(tmp_path)), "ordo", [], whole_stack=True, with_profiles=True,
                            force_recreate=False, dry_run=False, build=True) == 1
    assert no_docker == []                                            # compose never ran


def test_cli_up_builds_by_default_and_no_build_turns_it_off(monkeypatch, tmp_path, no_docker):
    from ordo import cli

    monkeypatch.setattr(cli, "_host_preflight", lambda *a, **k: True)  # tested in test_preflight_host.py
    seen = _capture_ensure(monkeypatch)
    out = str(_out(tmp_path))
    assert cli.main(["up", "--all", "--out", out]) == 0
    assert cli.main(["up", "--all", "--out", out, "--no-build"]) == 0
    assert len(seen) == 1


def test_an_upstream_llamacpp_image_is_never_retagged_by_the_record(tmp_path):
    """Without a special build, llama.cpp runs the backend selector's digest-pinned upstream image.
    It is not an `ordo build` image, so no record entry, even one naming it, changes what render writes."""
    hardware = {"gpus": [], "ram_gb": 32, "cpu_cores": 8, "platform": "Linux"}
    rc = render(Source.from_dict({"hardware": hardware, "model": "auto", "plugins": "auto"}), CATALOG)
    expected = rc.compose_dict()["services"]["llamacpp"]["image"]
    assert "@sha256:" in expected
    images.save_record(tmp_path, {"ghcr.io/ggml-org/llama.cpp": "b" * 12,
                                  buildspec.image_ident(expected): "c" * 12})
    rc.write(tmp_path)
    assert _compose_images(tmp_path)["llamacpp"] == expected


def test_the_patched_llamacpp_build_is_pinned_to_its_recorded_tag(tmp_path):
    """The patched sm_120 build a catalog `backend_image` names is first-party: `ordo build` builds it
    from services/llamacpp-patched and records the tag, and render pins the compose to it like every
    other first-party image."""
    rc = _rc(PROFILE_5090_SM120)
    assert rc.model.backend_image == "ordo/llamacpp-patched"
    rc.write(tmp_path)
    assert _compose_images(tmp_path)["llamacpp"] == "ordo/llamacpp-patched:current"
    images.save_record(tmp_path, {"ordo/llamacpp-patched": "a" * 12})
    rc.write(tmp_path)
    assert _compose_images(tmp_path)["llamacpp"] == "ordo/llamacpp-patched:" + "a" * 12


def test_ordo_build_selects_the_patched_llamacpp_build(tmp_path):
    rc = _rc(PROFILE_5090_SM120)
    rc.write(tmp_path)
    doc = yaml.safe_load((tmp_path / "docker-compose.yml").read_text(encoding="utf-8"))
    assert images.select_images(doc, FIRST_PARTY, ["llamacpp"]) == ["ordo/llamacpp-patched"]
    assert "ordo/llamacpp-patched" in images.select_images(doc, FIRST_PARTY, None)
    target = images.build_target("ordo/llamacpp-patched", FIRST_PARTY["ordo/llamacpp-patched"], ROOT)
    assert (target.context, target.dockerfile) == ("services/llamacpp-patched", "services/llamacpp-patched/Dockerfile")


def test_every_catalog_backend_image_is_a_first_party_build():
    """A special llama.cpp build is built by `ordo build`, never by hand under a hand-picked tag: its
    catalog `backend_image` names an untagged first-party image, and render fills in the tag."""
    special = {m.id: m.backend_image for m in CATALOG.models if m.backend_image}
    assert special
    for model_id, ref in special.items():
        assert not images.has_tag(ref), f"{model_id}: {ref} carries its own tag; render owns it"
        assert ref in FIRST_PARTY, f"{model_id}: {ref} is not an image `ordo build` manages"
