"""`ordo backup` / `ordo restore` and the manifests' `backup:` declarations.

The documented restore used to extract a file the backup never wrote and never re-imported a
volume. These tests pin the declarations (every shipped volume has one), the plan (which volumes,
which method, which services stop) and the archive round trip. Nothing touches docker: the docker
calls go through a fake, and the lease reader is replaced.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest
import yaml

from ordo import cli
from ordo.host import backup, bringup
from ordo.render import backup_policy, compose
from ordo.render.agents import AgentRegistry
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.dashboards import DashboardRegistry
from ordo.render.engine import DEFAULT_AGENTS_DIR, DEFAULT_CATALOG, DEFAULT_DASHBOARDS_DIR, DEFAULT_PLUGINS_DIR, render
from ordo.render.plugins import Plugin, PluginRegistry

REPO = Path(__file__).resolve().parents[2]

COMPOSE = {
    "services": {
        "ops-controller": {"image": "ordo/ops-controller:abc",
                           "volumes": ["comfyui-models:/models/comfyui", "comfyui-app:/comfyui-app:ro"]},
        "litellm-db": {"image": "postgres:16-alpine@sha256:aa",
                       "environment": {"POSTGRES_USER": "${DB_USER:-litellm}", "POSTGRES_DB": "litellm"},
                       "volumes": ["litellm-db-data:/var/lib/postgresql/data"]},
        "model-gateway": {"image": "ordo/model-gateway:abc", "depends_on": {"litellm-db": {"condition": "x"}}},
        "qdrant": {"image": "qdrant/qdrant:v1.19.1", "profiles": ["rag"],
                   "volumes": ["qdrant-data:/qdrant/storage"]},
        "open-webui": {"image": "x", "depends_on": ["qdrant"]},
        "agent": {"image": "ordo/agent-hermes:abc", "volumes": ["hermes-home:/home/hermes/.hermes"]},
        "evals": {"image": "ordo/evals:abc", "volumes": ["hermes-home:/hermes-home:ro"]},
        "caddy": {"image": "caddy:2", "profiles": ["edge"], "volumes": ["caddy_data:/data"]},
        "tailnet-chat": {"image": "ts", "profiles": ["edge"], "network_mode": "service:caddy",
                         "volumes": [{"type": "volume", "source": "ts-state-chat", "target": "/var/lib/tailscale"}]},
        "comfyui": {"image": "comfy", "volumes": ["comfyui-app:/root", "comfyui-models:/root/models:ro"]},
    },
    "volumes": {"comfyui-models": None, "comfyui-app": None, "litellm-db-data": None, "qdrant-data": None,
                "hermes-home": None, "caddy_data": None, "ts-state-chat": {"name": "shared-ts-chat"}},
}
POLICY = {"comfyui-models": "skip", "comfyui-app": "live", "litellm-db-data": "pg_dump",
          "qdrant-data": "stopped", "hermes-home": "stopped", "caddy_data": "live", "ts-state-chat": "live"}


def _plans(policy=POLICY, only=None, doc=COMPOSE):
    return {p.volume: p for p in backup.plan(doc, policy, project="ordo", env={}, only=only)}


# ── the declarations ────────────────────────────────────────────────────────────────────────────


def _manifest_volumes() -> dict[str, set[str]]:
    """Every named volume a shipped manifest (or the core) mounts -> who mounts it."""
    found: dict[str, set[str]] = {}

    def add(owner: str, mounts) -> None:
        for mount in mounts:
            name = backup_policy.named_volume(str(mount))
            if name:
                found.setdefault(name, set()).add(owner)

    for plugin in PluginRegistry.load(DEFAULT_PLUGINS_DIR).plugins:
        add(f"plugin {plugin.id}", [v for s in plugin.services for v in s.volumes])
        add(f"plugin {plugin.id}", plugin.mcp.volumes if plugin.mcp else [])
    for agent in AgentRegistry.load(DEFAULT_AGENTS_DIR).agents:
        add(f"agent {agent.id}", agent.volumes)
    for dashboard in DashboardRegistry.load(DEFAULT_DASHBOARDS_DIR).dashboards:
        add(f"dashboard {dashboard.id}", dashboard.volumes)
    return found


def _all_declarations() -> dict[str, str]:
    return backup_policy.collect([
        ("core", compose.CORE_BACKUP),
        *((f"agent {a.id}", a.backup) for a in AgentRegistry.load(DEFAULT_AGENTS_DIR).agents),
        *((f"plugin {p.id}", p.backup) for p in PluginRegistry.load(DEFAULT_PLUGINS_DIR).plugins),
    ])


def test_every_shipped_volume_declares_how_it_is_backed_up():
    declared = _all_declarations()  # also: no volume is declared twice
    undeclared = {volume: sorted(owners) for volume, owners in _manifest_volumes().items() if volume not in declared}
    assert not undeclared, f"add a `backup:` declaration for {undeclared} (ordo/render/backup_policy.py)"


def test_the_databases_are_dumped_or_snapshotted_stopped():
    declared = _all_declarations()
    assert declared["litellm-db-data"] == declared["langfuse-db-data"] == "pg_dump"
    for volume in ("qdrant-data", "couchdb-data", "langfuse-clickhouse-data", "langfuse-redis-data",
                   "n8n-data", "open-webui-data", "hermes-home", "grafana-data", "langfuse-minio-data"):
        assert declared[volume] == "stopped", volume


def test_the_render_carries_the_declarations_into_the_manifest():
    rendered = render(Source.load(REPO / "ordo.example.yaml"), Catalog.load(DEFAULT_CATALOG))
    manifest = rendered.manifest()
    volumes = set(rendered.compose_dict().get("volumes") or {})
    assert set(manifest["backup"]) == volumes
    assert manifest["backup"]["litellm-db-data"] == "pg_dump"


def test_a_manifest_may_declare_only_the_volumes_its_services_mount():
    base = {"id": "p", "services": [{"name": "s", "image": "i", "volumes": ["own:/data", "${X}/bind:/b"]}]}
    assert Plugin.from_dict({**base, "backup": {"own": "stopped"}}).backup == {"own": "stopped"}
    with pytest.raises(ValueError, match="none of its services mount"):
        Plugin.from_dict({**base, "backup": {"other": "stopped"}})
    with pytest.raises(ValueError, match="must be one of"):
        Plugin.from_dict({**base, "backup": {"own": "rsync"}})
    with pytest.raises(ValueError, match="mapping"):
        Plugin.from_dict({**base, "backup": ["own"]})


def test_a_volume_declared_twice_is_refused():
    with pytest.raises(ValueError, match="twice"):
        backup_policy.collect([("core", {"v": "skip"}), ("plugin p", {"v": "stopped"})])


# ── the plan ────────────────────────────────────────────────────────────────────────────────────


def test_plan_reads_each_volume_method_and_who_writes_it():
    plans = _plans()
    assert plans["comfyui-app"].writers == ("comfyui",) and plans["comfyui-app"].readers == ("ops-controller",)
    assert plans["hermes-home"].writers == ("agent",) and plans["hermes-home"].readers == ("evals",)
    assert plans["hermes-home"].stopped_for_backup() == ("agent",)
    assert plans["caddy_data"].stopped_for_backup() == ()          # live: nothing stops for a backup
    assert plans["caddy_data"].stopped_for_restore() == ("caddy",)  # ...but a restore stops the writer
    assert plans["comfyui-models"].member == "" and plans["qdrant-data"].member == "volumes/qdrant-data.tar.gz"


def test_plan_finds_the_database_server_and_its_clients():
    db = _plans()["litellm-db-data"]
    assert (db.database_service, db.database_user, db.database_name) == ("litellm-db", "litellm", "litellm")
    assert db.clients == ("model-gateway",)
    assert db.stopped_for_backup() == () and db.stopped_for_restore() == ("model-gateway",)
    assert db.member == "databases/litellm-db-data.pgdump"


def test_plan_uses_the_compose_volume_name():
    plans = _plans()
    assert plans["qdrant-data"].docker_volume == "ordo_qdrant-data"
    assert plans["ts-state-chat"].docker_volume == "shared-ts-chat"


def test_an_undeclared_volume_is_snapshotted_stopped():
    plans = _plans(policy={k: v for k, v in POLICY.items() if k != "qdrant-data"})
    assert plans["qdrant-data"].method == "stopped" and not plans["qdrant-data"].declared


def test_only_keeps_the_volumes_those_services_write():
    assert set(_plans(only=["agent"])) == {"hermes-home"}
    assert set(_plans(only=["litellm-db", "qdrant"])) == {"litellm-db-data", "qdrant-data"}
    with pytest.raises(backup.BackupError, match="no such service"):
        _plans(only=["nope"])


def test_only_never_selects_a_volume_through_a_read_only_mounter():
    # evals mounts hermes-home read-only: `--only evals` must not wipe the agent's brain.
    assert set(_plans(only=["evals", "agent"])) == {"hermes-home"}
    with pytest.raises(backup.BackupError, match="write no volume"):
        _plans(only=["evals"])


def test_a_plan_never_stops_ops_controller():
    # comfyui-models undeclared -> stopped -> would stop its read-write mounter, ops-controller.
    with pytest.raises(backup.BackupError, match="ops-controller"):
        _plans(policy={k: v for k, v in POLICY.items() if k != "comfyui-models"})


def test_a_dump_needs_exactly_one_server():
    doc = yaml.safe_load(yaml.safe_dump(COMPOSE))
    doc["services"]["qdrant"]["volumes"].append("litellm-db-data:/x")
    with pytest.raises(backup.BackupError, match="exactly one server"):
        _plans(doc=doc)


def test_stopping_a_netns_owner_stops_its_members():
    assert backup.stop_set(COMPOSE, ["caddy"]) == ["caddy", "tailnet-chat"]


# ── the destination ─────────────────────────────────────────────────────────────────────────────


def test_a_backup_is_never_written_inside_the_checkout(tmp_path):
    with pytest.raises(backup.BackupError, match="checkout"):
        backup.check_destination(REPO / "backups", tmp_path / "out")
    with pytest.raises(backup.BackupError, match="stack"):
        backup.check_destination(tmp_path / "out" / "bk", tmp_path / "out")
    backup.check_destination(tmp_path / "elsewhere", tmp_path / "out")


# ── the round trip, against a fake docker ───────────────────────────────────────────────────────


class FakeDocker:
    """Volumes are byte strings, the database is a byte string, and every call is logged."""

    def __init__(self, doc, running):
        self.doc = doc
        self.running = set(running)
        self.volumes = {"ordo_qdrant-data": b"qdrant-bytes", "ordo_hermes-home": b"hermes-bytes",
                        "ordo_comfyui-app": b"app-bytes", "ordo_caddy_data": b"cert-bytes"}
        self.database = b"pg-dump-bytes"
        self.temp_database: bytes | None = None
        self.server_major, self.dump_major = 16, 16
        self.fail_stop: set[str] = set()      # `compose stop <service>` fails
        self.fail_replace: set[str] = set()   # the unpack into this volume fails (contents unchanged)
        self.fail_temp_restore = False        # pg_restore into the temporary database fails
        self.log: list[tuple] = []

    def running_container(self, service):
        return f"ordo-{service}-1" if service in self.running else None

    def compose(self, verb, *services):
        self.log.append((verb, *services))
        if verb == "stop" and self.fail_stop & set(services):
            raise backup.BackupError(f"docker compose stop failed for {services}")
        if verb == "stop":
            self.running -= set(services)
        elif verb == "start":
            self.running |= set(services)

    def volume_exists(self, docker_volume):
        return docker_volume in self.volumes

    def create_volume(self, docker_volume, volume):
        self.log.append(("create", docker_volume))
        self.volumes[docker_volume] = b""

    def image_of(self, service):
        return {"service": service, "image": COMPOSE["services"][service]["image"], "image_id": "sha256:1"}

    def snapshot_volume(self, docker_volume, dest):
        self.log.append(("snapshot", docker_volume, frozenset(self.running)))
        dest.write(self.volumes[docker_volume])

    def replace_volume(self, docker_volume, source):
        self.log.append(("replace", docker_volume, frozenset(self.running)))
        data = source.read()
        if docker_volume in self.fail_replace:
            raise backup.BackupError(f"restore of {docker_volume} failed: the unpack failed; the volume is unchanged")
        self.volumes[docker_volume] = data

    def pg_dump(self, container, user, database, dest):
        self.log.append(("pg_dump", container, user, database))
        dest.write(self.database)

    def pg_server_major(self, container, user):
        return self.server_major

    def pg_dump_major(self, container, user, source):
        source.read()
        return self.dump_major

    def pg_restore_to_temp(self, container, user, database, source):
        self.log.append(("pg_restore_to_temp", container, frozenset(self.running)))
        data = source.read()
        if self.fail_temp_restore:
            raise backup.BackupError(f"pg_restore of {database} failed; {database} is unchanged")
        self.temp_database = data

    def pg_swap(self, container, user, database):
        self.log.append(("pg_swap", container, frozenset(self.running)))
        self.database, self.temp_database = self.temp_database, None

    def wait_pg_ready(self, container, user, database):
        pass

    def bring_up(self, services):
        self.log.append(("up", *services))
        self.running |= set(services)


@pytest.fixture
def stack_dir(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "docker-compose.yml").write_text(yaml.safe_dump(COMPOSE), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"backup": POLICY, "substrate_digest": "d1"}), encoding="utf-8")
    (out / "ordo.yaml").write_bytes(b"model: auto\n")
    (out / "secrets.env").write_bytes(b"KEY=value\n")
    return out


@pytest.fixture(autouse=True)
def no_lease(monkeypatch):
    monkeypatch.setattr(bringup, "read_gpu_status", lambda project: None)


RUNNING = ["litellm-db", "model-gateway", "qdrant", "agent", "caddy", "tailnet-chat", "comfyui", "ops-controller"]


def _backup(stack_dir, tmp_path, docker, only=None):
    return backup.run_backup(stack_dir=stack_dir, project="ordo", dest=tmp_path / "backups", only=only,
                             dry_run=False, docker=docker)


def test_backup_writes_one_archive_with_a_manifest_and_checksums(stack_dir, tmp_path):
    docker = FakeDocker(COMPOSE, RUNNING)
    archive = _backup(stack_dir, tmp_path, docker)
    manifest = backup.read_manifest(archive)
    assert manifest["project"] == "ordo" and manifest["contains_secrets"] is True
    assert manifest["substrate_digest"] == "d1"
    assert [c["file"] for c in manifest["config"]] == ["ordo.yaml", "secrets.env"]
    status = {v["volume"]: v["status"] for v in manifest["volumes"]}
    assert status == {"comfyui-models": "skipped", "comfyui-app": "saved", "litellm-db-data": "saved",
                      "qdrant-data": "saved", "hermes-home": "saved", "caddy_data": "saved",
                      "ts-state-chat": "absent"}
    db = next(v for v in manifest["volumes"] if v["volume"] == "litellm-db-data")
    assert db["images"][0]["image"] == "postgres:16-alpine@sha256:aa"
    backup.verify(archive, [v["member"] for v in manifest["volumes"] if v["status"] == "saved"])
    with tarfile.open(archive) as tar:
        assert tar.extractfile("databases/litellm-db-data.pgdump").read() == b"pg-dump-bytes"
        assert tar.extractfile("config/secrets.env").read() == b"KEY=value\n"
        assert {m.mode for m in tar.getmembers()} == {0o600}
    assert not list((tmp_path / "backups").glob(".*"))  # no partial or scratch file left behind


def test_backup_stops_the_writers_of_a_stopped_volume_and_starts_them_again(stack_dir, tmp_path):
    docker = FakeDocker(COMPOSE, RUNNING)
    _backup(stack_dir, tmp_path, docker)
    snapshot = next(e for e in docker.log if e[:2] == ("snapshot", "ordo_hermes-home"))
    assert "agent" not in snapshot[2]
    i = docker.log.index(snapshot)
    assert docker.log[i - 1] == ("stop", "agent") and docker.log[i + 1] == ("start", "agent")
    live = next(e for e in docker.log if e[:2] == ("snapshot", "ordo_caddy_data"))
    assert "caddy" in live[2]  # live: left running
    assert "ops-controller" in docker.running and not any("ops-controller" in e for e in docker.log)


def test_backup_does_not_start_a_service_that_was_stopped(stack_dir, tmp_path):
    docker = FakeDocker(COMPOSE, [s for s in RUNNING if s != "qdrant"])
    _backup(stack_dir, tmp_path, docker)
    assert not any(e[0] in ("stop", "start") and "qdrant" in e for e in docker.log)


def test_backup_only_leaves_the_config_out(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING), only=["qdrant"])
    manifest = backup.read_manifest(archive)
    assert manifest["config"] == [] and [v["volume"] for v in manifest["volumes"]] == ["qdrant-data"]


def test_backup_refuses_a_dump_of_a_stopped_database(stack_dir, tmp_path):
    with pytest.raises(backup.BackupError, match="litellm-db is not running"):
        _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, [s for s in RUNNING if s != "litellm-db"]))


def _restore(archive, stack_dir, docker, **kw):
    return backup.run_restore(archive=archive, stack_dir=stack_dir, project="ordo", only=kw.pop("only", None),
                              dry_run=kw.pop("dry_run", False), docker=docker, **kw)


def test_restore_puts_every_volume_and_the_database_back(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    docker = FakeDocker(COMPOSE, RUNNING)
    docker.volumes = {name: b"lost" for name in docker.volumes}
    docker.database = b"lost"
    assert _restore(archive, stack_dir, docker) == 0
    assert docker.volumes == {"ordo_qdrant-data": b"qdrant-bytes", "ordo_hermes-home": b"hermes-bytes",
                              "ordo_comfyui-app": b"app-bytes", "ordo_caddy_data": b"cert-bytes"}
    assert docker.database == b"pg-dump-bytes"
    to_temp = next(e for e in docker.log if e[0] == "pg_restore_to_temp")
    assert "model-gateway" in to_temp[2]  # the load into the temporary database runs beside the clients
    swap = next(e for e in docker.log if e[0] == "pg_swap")
    assert "model-gateway" not in swap[2] and "litellm-db" in swap[2]  # the swap runs with them stopped
    caddy = next(e for e in docker.log if e[:2] == ("replace", "ordo_caddy_data"))
    assert not {"caddy", "tailnet-chat"} & caddy[2]  # a live volume's writer (and its netns) is stopped
    app = next(e for e in docker.log if e[:2] == ("replace", "ordo_comfyui-app"))
    assert "ops-controller" in app[2]  # a read-only mounter keeps running
    assert docker.running == set(RUNNING)
    assert _restore(archive, stack_dir, docker) == 0  # idempotent
    assert docker.database == b"pg-dump-bytes"


def test_restore_creates_a_missing_volume(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    docker = FakeDocker(COMPOSE, RUNNING)
    del docker.volumes["ordo_qdrant-data"]
    assert _restore(archive, stack_dir, docker, only=["qdrant"]) == 0
    assert ("create", "ordo_qdrant-data") in docker.log and docker.volumes["ordo_qdrant-data"] == b"qdrant-bytes"


def test_restore_starts_a_stopped_database_and_stops_it_after(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    docker = FakeDocker(COMPOSE, [s for s in RUNNING if s != "litellm-db"])
    assert _restore(archive, stack_dir, docker, only=["litellm-db"]) == 0
    assert ("up", "litellm-db") in docker.log and docker.log[-1] == ("stop", "litellm-db")


def test_restore_refuses_while_the_gpu_is_leased(stack_dir, tmp_path, monkeypatch):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    monkeypatch.setattr(bringup, "read_gpu_status", lambda project: {"leased": True, "running": [], "evicted_residents": {}})
    docker = FakeDocker(COMPOSE, RUNNING)
    assert _restore(archive, stack_dir, docker) == 2
    assert docker.log == []


def test_restore_refuses_when_the_lease_state_is_unknown(stack_dir, tmp_path, monkeypatch):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))

    def unknown(project):
        raise bringup.LeaseUnknown("docker is not answering")

    monkeypatch.setattr(bringup, "read_gpu_status", unknown)
    docker = FakeDocker(COMPOSE, RUNNING)
    assert _restore(archive, stack_dir, docker) == 2 and docker.log == []


def test_restore_dry_run_changes_nothing(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    (stack_dir / "secrets.env").unlink()
    docker = FakeDocker(COMPOSE, RUNNING)
    assert _restore(archive, stack_dir, docker, dry_run=True) == 0
    assert docker.log == [] and not (stack_dir / "secrets.env").exists()


def test_restore_refuses_a_file_snapshot_from_a_different_image(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    doc = yaml.safe_load(yaml.safe_dump(COMPOSE))
    doc["services"]["qdrant"]["image"] = "qdrant/qdrant:v2.0.0"
    (stack_dir / "docker-compose.yml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(backup.BackupError, match="different image"):
        _restore(archive, stack_dir, FakeDocker(doc, RUNNING), only=["qdrant"])
    assert _restore(archive, stack_dir, FakeDocker(doc, RUNNING), only=["qdrant"], allow_image_change=True) == 0


def test_restore_refuses_a_damaged_archive(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    damaged = tmp_path / "damaged.tar"
    with tarfile.open(archive) as src, tarfile.open(damaged, "w") as dst:
        for member in src.getmembers():
            data = src.extractfile(member).read()
            if member.name == "volumes/qdrant-data.tar.gz":
                data = b"tampered"
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data))
    docker = FakeDocker(COMPOSE, RUNNING)
    with pytest.raises(backup.BackupError, match="sha256"):
        _restore(damaged, stack_dir, docker)
    assert docker.log == []


def test_restore_refuses_another_project(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    with pytest.raises(backup.BackupError, match="project"):
        backup.run_restore(archive=archive, stack_dir=stack_dir, project="other", only=None, dry_run=True)


def test_restore_writes_absent_config_and_never_overwrites_present_config(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    (stack_dir / "secrets.env").unlink()
    (stack_dir / "ordo.yaml").write_text("model: other\n", encoding="utf-8")
    assert _restore(archive, stack_dir, FakeDocker(COMPOSE, RUNNING)) == 0
    assert (stack_dir / "secrets.env").read_bytes() == b"KEY=value\n"
    assert (stack_dir / "ordo.yaml").read_text(encoding="utf-8") == "model: other\n"


def test_restore_without_a_rendered_stack_restores_config_then_asks_for_a_render(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    fresh = tmp_path / "fresh"
    assert backup.run_restore(archive=archive, stack_dir=fresh, project="ordo", only=None, dry_run=False) == 1
    assert (fresh / "ordo.yaml").exists() and (fresh / "secrets.env").exists()


# ── the CLI ─────────────────────────────────────────────────────────────────────────────────────


def test_cli_routes_backup_and_restore(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(backup, "run_backup", lambda **kw: seen.setdefault("backup", kw))
    monkeypatch.setattr(backup, "run_restore", lambda **kw: seen.setdefault("restore", kw) and 0)
    assert cli.main(["backup", "--out", str(tmp_path), "--only", "qdrant", "--dry-run"]) == 0
    assert seen["backup"]["dest"] == tmp_path and seen["backup"]["only"] == ["qdrant"]
    assert seen["backup"]["stack_dir"] == Path("out") and seen["backup"]["dry_run"] is True
    assert cli.main(["restore", "a.tar", "--stack", "x", "--project", "p"]) == 0
    assert seen["restore"]["archive"] == Path("a.tar") and seen["restore"]["project"] == "p"


def test_cli_reports_a_refusal_as_exit_1(monkeypatch, tmp_path, capsys):
    def refuse(**kw):
        raise backup.BackupError("nope")

    monkeypatch.setattr(backup, "run_backup", refuse)
    assert cli.main(["backup", "--out", str(tmp_path)]) == 1
    assert "ordo backup: nope" in capsys.readouterr().err


def test_a_restore_streams_an_archive_member_into_the_process(tmp_path):
    """A tar member is not an OS file (no fileno), so it cannot be a subprocess's stdin directly: the
    restore pipes it through. Checked with a real process standing in for `docker exec -i`."""
    import sys

    archive = tmp_path / "a.tar"
    payload = b"x" * (3 << 20)  # larger than any pipe buffer
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("member")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    docker = backup.Docker(tmp_path, "p", {})
    reader = [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"]
    with tarfile.open(archive) as tar:
        docker._stream_in(reader, tar.extractfile("member"), "count")
    failing = [sys.executable, "-c", "import sys; sys.stdin.close(); print('refused'); sys.exit(3)"]
    with tarfile.open(archive) as tar, pytest.raises(backup.BackupError, match="exit 3.*refused"):
        docker._stream_in(failing, tar.extractfile("member"), "count")


# ── failures leave the data as it was, and say so ──────────────────────────────────────────────


def test_a_failed_file_restore_leaves_its_writers_stopped(stack_dir, tmp_path, capsys):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    docker = FakeDocker(COMPOSE, RUNNING)
    docker.volumes["ordo_hermes-home"] = b"current"
    docker.fail_replace = {"ordo_hermes-home"}
    with pytest.raises(backup.BackupError, match="unchanged"):
        _restore(archive, stack_dir, docker, only=["agent"])
    assert docker.volumes["ordo_hermes-home"] == b"current"
    assert "agent" not in docker.running and not any(e[0] == "start" for e in docker.log)
    err = capsys.readouterr().err
    assert "left stopped: agent" in err and "ordo up agent" in err


def test_a_failed_backup_snapshot_still_restarts_the_writers(stack_dir, tmp_path):
    docker = FakeDocker(COMPOSE, RUNNING)

    def broken(docker_volume, dest):
        raise backup.BackupError("snapshot failed")

    docker.snapshot_volume = broken
    with pytest.raises(backup.BackupError, match="snapshot failed"):
        _backup(stack_dir, tmp_path, docker, only=["agent"])
    assert "agent" in docker.running  # a backup changed nothing, so the service comes back


def test_a_partial_stop_failure_restarts_what_it_stopped(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    docker = FakeDocker(COMPOSE, RUNNING)
    docker.fail_stop = {"tailnet-chat"}
    with pytest.raises(backup.BackupError, match="stop failed"):
        _restore(archive, stack_dir, docker, only=["caddy"])
    assert ("stop", "caddy") in docker.log and ("start", "caddy") in docker.log
    assert {"caddy", "tailnet-chat"} <= docker.running
    assert docker.volumes["ordo_caddy_data"] == b"cert-bytes" and not any(e[0] == "replace" for e in docker.log)


def test_a_dump_that_does_not_load_leaves_the_database_and_its_clients_alone(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    docker = FakeDocker(COMPOSE, RUNNING)
    docker.database = b"live-data"
    docker.fail_temp_restore = True
    with pytest.raises(backup.BackupError, match="unchanged"):
        _restore(archive, stack_dir, docker, only=["litellm-db"])
    assert docker.database == b"live-data" and not any(e[0] in ("pg_swap", "stop") for e in docker.log)
    assert "model-gateway" in docker.running


def test_a_dump_from_a_newer_postgres_is_refused_before_anything_changes(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    docker = FakeDocker(COMPOSE, RUNNING)
    docker.database = b"live-data"
    docker.dump_major, docker.server_major = 16, 15
    with pytest.raises(backup.BackupError, match="PostgreSQL 16.*15"):
        _restore(archive, stack_dir, docker, only=["litellm-db"])
    assert docker.database == b"live-data" and not any(e[0] in ("pg_restore_to_temp", "stop") for e in docker.log)


def test_a_dump_from_a_different_image_needs_allow_image_change(stack_dir, tmp_path):
    archive = _backup(stack_dir, tmp_path, FakeDocker(COMPOSE, RUNNING))
    doc = yaml.safe_load(yaml.safe_dump(COMPOSE))
    doc["services"]["litellm-db"]["image"] = "postgres:17-alpine@sha256:bb"
    (stack_dir / "docker-compose.yml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(backup.BackupError, match="different image"):
        _restore(archive, stack_dir, FakeDocker(doc, RUNNING), only=["litellm-db"])
    assert _restore(archive, stack_dir, FakeDocker(doc, RUNNING), only=["litellm-db"], allow_image_change=True) == 0


# ── the volume swap script, run by a real shell against a real directory ───────────────────────


def _snapshot(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _run_swap(tmp_path, stdin: bytes):
    import shutil
    import subprocess

    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell")
    # A relative path: GNU tar reads `C:/...` as a remote host on Windows.
    return subprocess.run([shell, "-c", backup.REPLACE_VOLUME_SCRIPT, "sh", "vol"], input=stdin,
                          capture_output=True, cwd=tmp_path, env={**os.environ, "LC_ALL": "C"})


def _contents(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def volume(tmp_path) -> Path:
    vol = tmp_path / "vol"
    (vol / "sub").mkdir(parents=True)
    (vol / "current.txt").write_bytes(b"current")
    (vol / ".hidden").write_bytes(b"dot")
    (vol / "sub" / "nested").write_bytes(b"n")
    return vol


def test_the_swap_replaces_every_entry_including_dotfiles(tmp_path, volume):
    proc = _run_swap(tmp_path, _snapshot({"restored.txt": b"r", ".restored-dot": b"d"}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _contents(volume) == {"restored.txt": b"r", ".restored-dot": b"d"}


def test_an_unpack_that_fails_midway_leaves_the_volume_unchanged(tmp_path, volume):
    before = _contents(volume)
    whole = _snapshot({f"f{i}": os.urandom(200_000) for i in range(5)})
    proc = _run_swap(tmp_path, whole[: len(whole) * 6 // 10])
    assert proc.returncode == backup.SWAP_UNCHANGED, proc.stdout + proc.stderr
    assert _contents(volume) == before


def test_the_swap_refuses_a_volume_a_crashed_restore_left_behind(tmp_path, volume):
    (volume / backup.SWAP_PREVIOUS_DIR).mkdir()
    before = _contents(volume)
    proc = _run_swap(tmp_path, _snapshot({"restored.txt": b"r"}))
    assert proc.returncode == backup.SWAP_LEFTOVER
    assert _contents(volume) == before
