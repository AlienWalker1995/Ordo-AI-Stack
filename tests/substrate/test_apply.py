"""`ordo apply`: one command that deploys the checkout and the operator source in the right order.

A deploy used to be several commands the operator had to order by hand (`ordo build`, render,
`ordo secrets materialize`, `ordo recreate ops-controller` when ordo/ changed, `ordo up`, `ordo
doctor`), and each wrong order has bitten: a render before the build names tags that do not exist,
recreating services before ops-controller trips its substrate-digest guard, and a recreate during
a GPU lease is refused halfway through. These tests pin the order, the changed-set computation
(config hash AND image id, compared by one compose version), the dry run, the lease refusal and
the no-op. Docker is never touched: the host seam and the docker runner are fakes.
"""
from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from ordo import cli
from ordo.host import apply, bringup, doctor, images

OPS = bringup.OPS_CONTROLLER_SERVICE
COMPOSE_VERSION = "5.1.0"
CHECKOUT_DIGEST = "c" * 64

DOC = {
    "services": {
        "ops-controller": {"image": "ordo/ops-controller:new", "restart": "unless-stopped",
                           "environment": {bringup.SCHEDULER_STATE_KEY: "/data/scheduler-state.json"}},
        "llamacpp": {"image": "ordo/llamacpp-patched:abc", "restart": "unless-stopped"},
        "dashboard": {"image": "ordo/dashboard:new", "restart": "unless-stopped"},
        "caddy": {"image": "caddy:2", "restart": "unless-stopped", "profiles": ["edge"]},
        "tailnet-chat": {"image": "tailscale/tailscale:1", "restart": "unless-stopped",
                         "profiles": ["edge"], "network_mode": "service:caddy"},
        "evals": {"image": "ordo/evals:new", "restart": "no", "profiles": ["evals"]},
    }
}

IDLE = {"state": "idle", "leased": False, "running": [], "queued": [], "evicted_residents": {}}
LEASED = {"state": "busy", "leased": True, "running": [{"id": "gate-comfyui", "kind": "media"}],
          "queued": [], "evicted_residents": {"llamacpp": 27.5}, "state_persisted": True}


def rendered(name: str, config_hash: str = "h", image_id: str | None = "sha256:img", *,
             image_ref: str | None = None, one_shot: bool = False) -> apply.RenderedService:
    return apply.RenderedService(service=name, config_hash=config_hash,
                                 image_ref=image_ref or f"{name}:ref", image_id=image_id, one_shot=one_shot)


def running(name: str, config_hash: str = "h", image_id: str = "sha256:img",
            compose_version: str = COMPOSE_VERSION) -> apply.RunningContainer:
    return apply.RunningContainer(service=name, config_hash=config_hash, image_id=image_id,
                                  compose_version=compose_version)


def in_sync() -> tuple[dict, dict]:
    """A rendered stack whose every long-running service matches its running container."""
    names = [n for n, spec in DOC["services"].items() if spec.get("restart") != "no"]
    return ({n: rendered(n) for n in names} | {"evals": rendered("evals", one_shot=True)},
            {n: running(n) for n in names})


# --------------------------------------------------------------------------- #
# The changed set: config hash and image id, one compose version.
# --------------------------------------------------------------------------- #


def test_an_unchanged_stack_has_no_changes():
    want, have = in_sync()
    assert apply.diff_services(want, have, compose_version=COMPOSE_VERSION) == []


def test_a_changed_config_hash_is_a_change():
    want, have = in_sync()
    want["dashboard"] = rendered("dashboard", config_hash="h2")
    changes = apply.diff_services(want, have, compose_version=COMPOSE_VERSION)
    assert [c.service for c in changes] == ["dashboard"]
    assert "config" in changes[0].reasons[0]


def test_a_changed_image_id_under_the_same_ref_is_a_change():
    """A rebuilt `-dirty` tag or a moved upstream tag keeps the compose (and its hash) identical."""
    want, have = in_sync()
    want["caddy"] = rendered("caddy", image_id="sha256:other")
    changes = apply.diff_services(want, have, compose_version=COMPOSE_VERSION)
    assert [c.service for c in changes] == ["caddy"]
    assert "image" in changes[0].reasons[0]


def test_an_image_this_apply_builds_is_a_change():
    want, have = in_sync()
    want["dashboard"] = rendered("dashboard", image_id=None, image_ref="ordo/dashboard:new")
    changes = apply.diff_services(want, have, compose_version=COMPOSE_VERSION,
                                  built_refs={"ordo/dashboard:new"})
    assert [c.service for c in changes] == ["dashboard"]
    assert "built by this apply" in changes[0].reasons[0]


def test_a_service_with_no_container_is_a_change():
    want, have = in_sync()
    del have["dashboard"]
    changes = apply.diff_services(want, have, compose_version=COMPOSE_VERSION)
    assert [(c.service, c.reasons) for c in changes] == [("dashboard", ("not created",))]


def test_a_container_made_by_another_compose_version_is_a_change():
    """Compose versions normalise the config differently (#237), so a hash from another version is
    not comparable: the container is recreated by this host's compose, never compared across."""
    want, have = in_sync()
    have["dashboard"] = running("dashboard", compose_version="2.33.0")
    changes = apply.diff_services(want, have, compose_version=COMPOSE_VERSION)
    assert [c.service for c in changes] == ["dashboard"]
    assert "2.33.0" in changes[0].reasons[0] and COMPOSE_VERSION in changes[0].reasons[0]


def test_a_one_shot_job_is_never_started():
    """`restart: "no"` (the evals runner): recreating it with `up -d` would start a run."""
    want, have = in_sync()
    want["evals"] = rendered("evals", config_hash="h2", one_shot=True)
    assert apply.diff_services(want, have, compose_version=COMPOSE_VERSION) == []


def test_ops_controller_is_ordered_first():
    want, have = in_sync()
    for name in ("caddy", "dashboard", OPS):
        want[name] = rendered(name, config_hash="h2")
    changes = apply.diff_services(want, have, compose_version=COMPOSE_VERSION)
    assert [c.service for c in changes] == [OPS, "caddy", "dashboard"]


def test_a_substrate_digest_mismatch_alone_recreates_ops_controller():
    want, have = in_sync()
    changes = apply.diff_services(want, have, compose_version=COMPOSE_VERSION,
                                  running_substrate="a" * 64, checkout_substrate=CHECKOUT_DIGEST)
    assert [c.service for c in changes] == [OPS]
    assert "substrate" in changes[0].reasons[0]


def test_a_matching_substrate_digest_changes_nothing():
    want, have = in_sync()
    assert apply.diff_services(want, have, compose_version=COMPOSE_VERSION,
                               running_substrate=CHECKOUT_DIGEST, checkout_substrate=CHECKOUT_DIGEST) == []


# --------------------------------------------------------------------------- #
# Reading docker (mocked): the rendered side and the running side.
# --------------------------------------------------------------------------- #


class FakeDockerCli:
    """Answers the docker CLI calls DockerState makes, and records every argv."""

    def __init__(self, *, hashes: str = "", config: dict | None = None, images: dict[str, str] | None = None,
                 ps: str = "", inspect: list | None = None, version: str = COMPOSE_VERSION + "\n",
                 fail: str | None = None):
        self.hashes, self.config, self.images = hashes, config or {}, images or {}
        self.ps, self.inspect, self.version, self.fail = ps, inspect or [], version, fail
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if self.fail and self.fail in joined:
            return subprocess.CompletedProcess(argv, 1, "", "Cannot connect to the Docker daemon")
        if "config --hash" in joined:
            return subprocess.CompletedProcess(argv, 0, self.hashes, "")
        if "config --format json" in joined:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.config), "")
        if argv[:3] == ["docker", "image", "inspect"]:
            ref = argv[-1]
            if ref in self.images:
                return subprocess.CompletedProcess(argv, 0, self.images[ref] + "\n", "")
            return subprocess.CompletedProcess(argv, 1, "", f"Error response from daemon: No such image: {ref}")
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, self.ps, "")
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.inspect), "")
        if "compose version" in joined:
            return subprocess.CompletedProcess(argv, 0, self.version, "")
        raise AssertionError(f"unexpected docker call: {argv}")


def _container(service: str, config_hash: str, image_id: str, *, version: str = COMPOSE_VERSION,
               oneoff: str = "False", container_id: str = "") -> dict:
    return {"Id": container_id or f"{service}-id", "Image": image_id, "Config": {"Labels": {
        "com.docker.compose.project": "ordo", "com.docker.compose.service": service,
        "com.docker.compose.config-hash": config_hash, "com.docker.compose.version": version,
        "com.docker.compose.oneoff": oneoff}}}


def test_rendered_reads_hashes_and_image_ids_with_the_shared_compose_argv(tmp_path):
    cli_ = FakeDockerCli(
        hashes="dashboard aaa\nevals bbb\n",
        config={"services": {"dashboard": {"image": "ordo/dashboard:new", "restart": "unless-stopped"},
                             "evals": {"image": "ordo/evals:new", "restart": "no"}}},
        images={"ordo/dashboard:new": "sha256:d1"})
    state = apply.DockerState(run=cli_)
    got = state.rendered(apply.Staged(compose_dir=tmp_path.as_posix(), project_directory="C:/out", doc=DOC),
                         project="ordo", profiles=["edge", "evals"], containers={})
    assert got["dashboard"] == apply.RenderedService("dashboard", "aaa", "ordo/dashboard:new", "sha256:d1", False)
    assert got["evals"] == apply.RenderedService("evals", "bbb", "ordo/evals:new", None, True)
    hash_call = next(c for c in cli_.calls if "--hash" in c)
    # The same builder as every bring-up (both env files, every profile), plus the project directory
    # the running containers were created against (their relative binds resolve from it).
    assert hash_call[:len(bringup.compose_argv(tmp_path.as_posix(), "ordo", profiles=["edge", "evals"]))] == \
        bringup.compose_argv(tmp_path.as_posix(), "ordo", profiles=["edge", "evals"])
    assert ["--project-directory", "C:/out"] == hash_call[hash_call.index("--project-directory"):][:2]


def test_running_reads_labels_and_image_ids_and_skips_one_off_runs():
    cli_ = FakeDockerCli(ps="id1\nid2\nid3\n", inspect=[
        _container("dashboard", "aaa", "sha256:d1"),
        _container("ops-controller", "ccc", "sha256:o1", version="2.33.0"),
        _container("evals", "eee", "sha256:e1", oneoff="True"),
    ])
    got = apply.DockerState(run=cli_).running(project="ordo")
    assert got == {"dashboard": apply.RunningContainer("dashboard", "aaa", "sha256:d1", COMPOSE_VERSION,
                                                       "dashboard-id"),
                   "ops-controller": apply.RunningContainer("ops-controller", "ccc", "sha256:o1", "2.33.0",
                                                            "ops-controller-id")}
    ps = next(c for c in cli_.calls if c[:2] == ["docker", "ps"])
    assert "label=com.docker.compose.project=ordo" in ps and "-a" in ps


def test_no_containers_is_an_empty_running_set_without_an_inspect():
    cli_ = FakeDockerCli(ps="")
    assert apply.DockerState(run=cli_).running(project="ordo") == {}
    assert not any(c[:2] == ["docker", "inspect"] for c in cli_.calls)


@pytest.mark.parametrize("failing, read", [
    ("docker ps", lambda state, staged: state.running(project="ordo")),
    ("config --hash", lambda state, staged: state.rendered(staged, project="ordo", profiles=[], containers={})),
    ("config --format json",
     lambda state, staged: state.rendered(staged, project="ordo", profiles=[], containers={})),
    ("compose version", lambda state, staged: state.compose_version()),
])
def test_unreadable_docker_state_fails_closed(tmp_path, failing, read):
    cli_ = FakeDockerCli(hashes="dashboard aaa\n",
                         config={"services": {"dashboard": {"image": "ordo/dashboard:new"}}}, fail=failing)
    staged = apply.Staged(tmp_path.as_posix(), tmp_path.as_posix(), DOC)
    with pytest.raises(apply.StateUnknown):
        read(apply.DockerState(run=cli_), staged)


def test_a_hashed_service_missing_from_the_config_fails_closed(tmp_path):
    cli_ = FakeDockerCli(hashes="dashboard aaa\n", config={"services": {}})
    with pytest.raises(apply.StateUnknown):
        apply.DockerState(run=cli_).rendered(apply.Staged(tmp_path.as_posix(), tmp_path.as_posix(), DOC),
                                             project="ordo", profiles=[], containers={})


# --------------------------------------------------------------------------- #
# Shared namespaces: compose hashes a member with its owner's container id.
# --------------------------------------------------------------------------- #

CADDY_ID = "f87d0664f98bb2cc73340d3c9c15a01637bb58abbb175271fbfd336a15aa92d5"


def test_a_namespace_reference_resolves_to_the_owners_container_id():
    """compose's convergence (resolveSharedNamespaces) rewrites `service:<owner>` in network_mode,
    ipc and pid to `container:<owner container id>` before it hashes and labels the member."""
    services = {
        "caddy": {"image": "caddy:2"},
        "tailnet-chat": {"network_mode": "service:caddy"},
        "sidecar": {"ipc": "service:caddy", "pid": "service:caddy"},
        "pinned": {"network_mode": "container:abc"},
        "plain": {"network_mode": "bridge"},
    }
    containers = {"caddy": apply.RunningContainer("caddy", "h", "sha256:c", COMPOSE_VERSION, CADDY_ID)}
    assert apply.shared_namespace_overrides(services, containers) == {
        "tailnet-chat": {"network_mode": f"container:{CADDY_ID}"},
        "sidecar": {"ipc": f"container:{CADDY_ID}", "pid": f"container:{CADDY_ID}"},
    }


def test_a_reference_to_an_owner_with_no_container_is_left_unresolved():
    """No owner container: the owner is itself "not created", and its members follow it."""
    services = {"caddy": {}, "tailnet-chat": {"network_mode": "service:caddy"}}
    assert apply.shared_namespace_overrides(services, {}) == {}


class HashingComposeCli(FakeDockerCli):
    """`config --hash` answers like compose 5.1.0 did on the live stack (2026-09-25): a member's
    hash is the one on its container's label only when its network_mode is resolved to the owner's
    container id (the override file on the argv); with `service:caddy` it is a different hash."""

    LABEL_HASH = "746a33129e436b6094d38f0becd6ba0813daeeb30a6741e0c1be312fa67a7cdc"
    UNRESOLVED_HASH = "464df342ec3a5dbad5eecc1a65257ce58d0b41becea9ed04b623ca6fec6933a8"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.overrides_seen: list[dict] = []

    def __call__(self, argv):
        if "--hash" in argv:
            files = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-f"][1:]
            override = {}
            for path in files:
                override |= (yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}).get("services") or {}
            self.overrides_seen.append(override)
            member = override.get("tailnet-chat") or {}
            chat = self.LABEL_HASH if member.get("network_mode") == f"container:{CADDY_ID}" else self.UNRESOLVED_HASH
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, f"caddy ccc\ntailnet-chat {chat}\n", "")
        return super().__call__(argv)


def test_an_unchanged_netns_member_is_not_a_change(tmp_path):
    """Reproduction of the 2026-09-25 bug: every apply listed caddy's netns members as "config
    changed", because `config --hash` hashes `network_mode: service:caddy` while compose labels the
    container with the hash of `container:<caddy id>`. Fixture: tailnet-chat's real running labels."""
    cli_ = HashingComposeCli(
        config={"services": {"caddy": {"image": "caddy:2", "restart": "unless-stopped"},
                             "tailnet-chat": {"image": "tailscale/tailscale:1", "restart": "unless-stopped",
                                              "network_mode": "service:caddy"}}},
        images={"caddy:2": "sha256:c", "tailscale/tailscale:1": "sha256:t"},
        ps="id1\nid2\n",
        inspect=[_container("caddy", "ccc", "sha256:c", container_id=CADDY_ID),
                 _container("tailnet-chat", HashingComposeCli.LABEL_HASH, "sha256:t", container_id="d" * 64)])
    state = apply.DockerState(run=cli_)
    have = state.running(project="ordo")
    want = state.rendered(apply.Staged(tmp_path.as_posix(), tmp_path.as_posix(), DOC), project="ordo",
                          profiles=["edge"], containers=have)
    assert cli_.overrides_seen == [{"tailnet-chat": {"network_mode": f"container:{CADDY_ID}"}}]
    assert apply.diff_services(want, have, compose_version=COMPOSE_VERSION) == []


def test_a_member_created_against_an_older_owner_container_is_a_change(tmp_path):
    """The owner was recreated without its member (the member sits in a dead namespace): compose
    hashes the member against the owner's CURRENT id, which no longer matches its label."""
    cli_ = HashingComposeCli(
        config={"services": {"caddy": {"image": "caddy:2"},
                             "tailnet-chat": {"image": "tailscale/tailscale:1", "network_mode": "service:caddy"}}},
        images={"caddy:2": "sha256:c", "tailscale/tailscale:1": "sha256:t"},
        ps="id1\nid2\n",
        inspect=[_container("caddy", "ccc", "sha256:c", container_id="e" * 64),
                 _container("tailnet-chat", HashingComposeCli.LABEL_HASH, "sha256:t", container_id="d" * 64)])
    state = apply.DockerState(run=cli_)
    have = state.running(project="ordo")
    want = state.rendered(apply.Staged(tmp_path.as_posix(), tmp_path.as_posix(), DOC), project="ordo",
                          profiles=["edge"], containers=have)
    changes = apply.diff_services(want, have, compose_version=COMPOSE_VERSION)
    assert [(c.service, c.reasons) for c in changes] == [("tailnet-chat", ("config changed",))]


def test_an_image_inspect_error_other_than_missing_fails_closed():
    def run(argv):
        return subprocess.CompletedProcess(argv, 1, "", "permission denied while trying to connect")
    with pytest.raises(apply.StateUnknown):
        apply.DockerState(run=run).image_id("ordo/dashboard:new")


# --------------------------------------------------------------------------- #
# The orchestration, against a fake host.
# --------------------------------------------------------------------------- #


class FakeHost:
    """Records every step `apply.run` takes. The read methods answer from the fields."""

    MUTATING = {"build", "write_render", "materialize_secrets", "preflight", "bring_up",
                "wait_for_ops_controller", "doctor"}

    def __init__(self, *, want: dict | None = None, have: dict | None = None, gpu: dict | None = None,
                 builds: list | None = None, substrate: str | None = CHECKOUT_DIGEST, bring_up_code: int = 0,
                 gpu_error: bool = False, state_error: bool = False, doc: dict = DOC):
        synced_want, synced_have = in_sync()
        self.want = synced_want if want is None else want
        self.have = synced_have if have is None else have
        self.gpu, self.gpu_error, self.state_error = (IDLE if gpu is None else gpu), gpu_error, state_error
        self.builds = builds or []
        self.substrate = substrate
        self.bring_up_code = bring_up_code
        self.doc = doc
        self.calls: list[tuple] = []
        self.project = "ordo"
        self.out = "out"
        self.source_path = "out/ordo.yaml"

    # reads
    def gpu_status(self):
        self.calls.append(("gpu_status",))
        if self.gpu_error:
            raise bringup.LeaseUnknown("docker is not answering")
        return self.gpu

    def rendered_doc(self):
        return self.doc

    def planned_builds(self, only):
        self.calls.append(("planned_builds", tuple(only or ())))
        return self.builds

    @contextmanager
    def staged_render(self, builds, *, dry_run):
        self.calls.append(("staged_render", dry_run))
        if not dry_run:
            self.calls.append(("write_render",))
        yield apply.Staged(compose_dir="stage", project_directory="out", doc=self.doc)

    def rendered_services(self, staged, containers):
        return self.want

    def running_containers(self):
        if self.state_error:
            raise apply.StateUnknown("docker ps failed")
        return self.have

    def compose_version(self):
        return COMPOSE_VERSION

    def running_substrate(self):
        return self.substrate

    def checkout_substrate(self):
        return CHECKOUT_DIGEST

    def secrets_store_description(self):
        return "out/secrets.env"

    # mutations
    def build(self, builds):
        self.calls.append(("build", tuple(b.ref for b in builds)))
        return 0

    def materialize_secrets(self):
        self.calls.append(("materialize_secrets",))
        return 0

    def preflight(self, services):
        self.calls.append(("preflight", tuple(services)))
        return True

    def bring_up(self, services, *, fetch_models):
        self.calls.append(("bring_up", tuple(services), fetch_models))
        return self.bring_up_code

    def wait_for_ops_controller(self):
        self.calls.append(("wait_for_ops_controller",))
        return True

    def doctor(self):
        self.calls.append(("doctor",))
        return 0

    def mutations(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in self.MUTATING]


def _changed(*names: str) -> dict:
    want, _ = in_sync()
    for name in names:
        want[name] = rendered(name, config_hash="h2")
    return want


def test_apply_runs_the_steps_in_order_with_ops_controller_first():
    host = FakeHost(want=_changed("dashboard", OPS, "caddy"))
    assert apply.run(host, only=None, dry_run=False) == 0
    steps = [c[0] for c in host.mutations()]
    assert steps == ["build", "write_render", "materialize_secrets", "preflight", "bring_up",
                     "wait_for_ops_controller", "bring_up", "doctor"]
    ups = [c for c in host.calls if c[0] == "bring_up"]
    assert ups[0] == ("bring_up", (OPS,), False)
    # caddy's netns members are expanded by bring_up itself (plan_named); apply names the changed set.
    assert ups[1] == ("bring_up", ("caddy", "dashboard"), True)


def test_preflight_checks_every_service_that_starts_including_netns_members():
    host = FakeHost(want=_changed("caddy"))
    apply.run(host, only=None, dry_run=False)
    assert ("preflight", ("caddy", "tailnet-chat")) in host.calls


def test_without_an_ops_controller_change_there_is_one_bring_up():
    host = FakeHost(want=_changed("dashboard"))
    assert apply.run(host, only=None, dry_run=False) == 0
    assert [c for c in host.calls if c[0] == "bring_up"] == [("bring_up", ("dashboard",), True)]
    assert ("wait_for_ops_controller",) not in host.calls


def test_a_substrate_change_recreates_ops_controller_first():
    host = FakeHost(substrate="a" * 64, want=_changed("dashboard"))
    assert apply.run(host, only=None, dry_run=False) == 0
    ups = [c for c in host.calls if c[0] == "bring_up"]
    assert ups == [("bring_up", (OPS,), False), ("bring_up", ("dashboard",), True)]


def test_a_failed_ops_controller_recreate_stops_before_the_rest():
    host = FakeHost(want=_changed("dashboard", OPS), bring_up_code=1)
    assert apply.run(host, only=None, dry_run=False) == 1
    assert [c for c in host.calls if c[0] == "bring_up"] == [("bring_up", (OPS,), False)]
    assert ("doctor",) not in host.calls


def test_nothing_changed_is_a_no_op_that_still_runs_doctor(capsys):
    host = FakeHost()
    assert apply.run(host, only=None, dry_run=False) == 0
    steps = [c[0] for c in host.mutations()]
    assert "bring_up" not in steps and "preflight" not in steps
    assert steps[-1] == "doctor"
    assert "nothing to recreate" in capsys.readouterr().out


def test_dry_run_prints_the_plan_and_changes_nothing(capsys):
    builds = [images.PlannedBuild(target=images.BuildTarget("ordo/dashboard", "d/Dockerfile", "d", ("d",)),
                                  commit="1" * 40, tag="111111111111", dirty=False, exists=False)]
    host = FakeHost(want=_changed("dashboard", OPS), builds=builds)
    assert apply.run(host, only=None, dry_run=True) == 0
    assert host.mutations() == []
    assert ("staged_render", True) in host.calls
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "ordo/dashboard:111111111111" in out
    assert out.index(f"recreate {OPS} first") < out.index("dashboard (config changed")
    assert "doctor" in out


def test_a_lease_that_would_be_violated_refuses_before_any_recreate(capsys):
    host = FakeHost(gpu=LEASED, want=_changed("llamacpp", "dashboard"))
    assert apply.run(host, only=None, dry_run=False) == 2
    assert not any(c[0] in ("bring_up", "preflight") for c in host.calls)
    assert "llamacpp" in capsys.readouterr().err


def test_a_lease_refusal_propagates_in_a_dry_run_too():
    host = FakeHost(gpu=LEASED, want=_changed("llamacpp"))
    assert apply.run(host, only=None, dry_run=True) == 2
    assert host.mutations() == []


def test_a_lease_that_the_changed_set_does_not_touch_is_no_refusal():
    host = FakeHost(gpu=LEASED, want=_changed("dashboard"))
    assert apply.run(host, only=None, dry_run=False) == 0


def test_a_bring_up_refused_mid_apply_propagates_its_code():
    host = FakeHost(want=_changed("dashboard"), bring_up_code=2)
    assert apply.run(host, only=None, dry_run=False) == 2


def test_an_unreadable_lease_refuses_before_building(capsys):
    host = FakeHost(gpu_error=True, want=_changed("dashboard"))
    assert apply.run(host, only=None, dry_run=False) == 2
    assert host.mutations() == []
    assert "unknown" in capsys.readouterr().err


def test_unreadable_container_state_refuses(capsys):
    host = FakeHost(state_error=True, want=_changed("dashboard"))
    assert apply.run(host, only=None, dry_run=False) == 2
    assert not any(c[0] == "bring_up" for c in host.calls)


def test_an_unreadable_substrate_digest_refuses():
    class Host(FakeHost):
        def running_substrate(self):
            raise doctor.SubstrateUnreadable("ops-controller /health timed out")
    host = Host(want=_changed("dashboard"))
    assert apply.run(host, only=None, dry_run=False) == 2
    assert not any(c[0] == "bring_up" for c in host.calls)


def test_only_limits_the_bring_up_but_keeps_a_changed_ops_controller_first(capsys):
    host = FakeHost(want=_changed("dashboard", "caddy", OPS))
    assert apply.run(host, only=["dashboard"], dry_run=False) == 0
    assert ("planned_builds", ("dashboard",)) in host.calls
    assert [c for c in host.calls if c[0] == "bring_up"] == [
        ("bring_up", (OPS,), False), ("bring_up", ("dashboard",), True)]
    assert "caddy" in capsys.readouterr().out   # reported as changed but left out


def test_only_an_unknown_service_is_an_error():
    host = FakeHost()
    assert apply.run(host, only=["nope"], dry_run=False) == 1
    assert host.mutations() == []


# --------------------------------------------------------------------------- #
# Build planning (images.plan_builds) and the CLI wiring.
# --------------------------------------------------------------------------- #


class _Git:
    def __init__(self, dirty=False):
        self.dirty = dirty

    def last_commit(self, paths):
        return "a" * 40

    def head(self):
        return "f" * 40

    def is_dirty(self, paths):
        return self.dirty


class _Docker:
    def __init__(self, present):
        self.present = set(present)

    def image_exists(self, ref):
        return ref in self.present


TARGET = images.BuildTarget("ordo/dashboard", "services/dashboard/Dockerfile", "services/dashboard",
                            ("services/dashboard",))


def test_plan_builds_skips_an_existing_clean_tag():
    [planned] = images.plan_builds([TARGET], git=_Git(), docker=_Docker({"ordo/dashboard:" + "a" * 12}))
    assert planned.ref == "ordo/dashboard:" + "a" * 12 and not planned.needs_build


def test_plan_builds_builds_a_missing_or_dirty_tag():
    [missing] = images.plan_builds([TARGET], git=_Git(), docker=_Docker(set()))
    [dirty] = images.plan_builds([TARGET], git=_Git(dirty=True),
                                 docker=_Docker({"ordo/dashboard:" + "a" * 12 + "-dirty"}))
    assert missing.needs_build and dirty.needs_build and dirty.tag.endswith("-dirty")


def test_cli_apply_parses_and_hands_off(monkeypatch, tmp_path):
    (tmp_path / "ordo.yaml").write_text("site: {}\n", encoding="utf-8")
    seen = {}

    def fake_run(host, *, only, dry_run):
        seen.update(host=host, only=only, dry_run=dry_run)
        return 0

    monkeypatch.setattr(apply, "run", fake_run)
    assert cli.main(["apply", "--out", str(tmp_path), "--dry-run", "--only", "dashboard", "caddy"]) == 0
    assert seen["only"] == ["dashboard", "caddy"] and seen["dry_run"] is True
    # With no --source, apply deploys the operator source in --out, never the public example.
    assert Path(seen["host"].source_path) == tmp_path / "ordo.yaml"


def test_cli_apply_without_a_source_in_out_refuses(tmp_path, capsys):
    assert cli.main(["apply", "--out", str(tmp_path), "--dry-run"]) == 1
    assert "ordo.yaml" in capsys.readouterr().err
