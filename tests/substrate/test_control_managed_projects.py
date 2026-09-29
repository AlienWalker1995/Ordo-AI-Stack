"""Hermes maintains the other compose projects on this host through ops-controller (SEC-1, Q3).

The owner's decision: Hermes may see the status of, tail the logs of, and restart containers of
an explicit list of OTHER compose projects (nas-stack, azerothcore, adguard on the reference
host), declared as `managed_projects:` in ordo.yaml. Nothing else: no stop, start, exec, create
or compose verbs. Every restart is confirm-gated (JSON `true`), rate-limited (3 per container per
hour, then 429), audited under the caller's principal, and refused for a container that could
reach the GPU the scheduler leases. Ordo's own project can never be listed, so no Ordo service,
and so no lease-managed resident, is reachable through these routes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ordo.control import managed, principals
from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Scheduler
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
ADMIN_TOKEN = "admin-token-managed"
HERMES_TOKEN = "hermes-token-managed"
HERMES = {"Authorization": f"Bearer {HERMES_TOKEN}", "X-Actor": "hermes:discord"}

LEASED = "GPU-97fe65ee-5e2d-8c9b-32d0-362f510ceb96"      # the primary card the scheduler arbitrates
OTHER = "GPU-20fac13a-5e5b-1818-581f-63901612fd84"       # the secondary card (NVENC, voice)
HARDWARE = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": LEASED, "compute_cap": "12.0"},
                     {"name": "GTX 1070", "vram_gb": 8, "uuid": OTHER, "compute_cap": "6.1"}],
            "ram_gb": 128}


def _raw(env=(), device_requests=None, runtime="runc"):
    return {"Config": {"Env": list(env)},
            "HostConfig": {"DeviceRequests": device_requests, "Runtime": runtime}}


ALL_GPUS = [{"Driver": "nvidia", "Count": -1, "DeviceIDs": None, "Capabilities": [["gpu"]]}]

FOREIGN = {
    "nas-stack": {
        "janitorr": ({"name": "janitorr", "service": "janitorr", "state": "running", "health": "unhealthy",
                      "status": "Up 3 hours (unhealthy)", "image": "ghcr.io/schaka/janitorr:jvm-v2.1.1"},
                     _raw(env=["JELLYFIN_API_KEY=sekrit-jf"])),
        # The live Jellyfin: all GPUs requested, pinned to the 1070 by the env pair.
        "jellyfin": ({"name": "jellyfin", "service": "jellyfin", "state": "running", "health": "healthy",
                      "status": "Up 2 days (healthy)", "image": "jellyfin/jellyfin"},
                     _raw(env=[f"NVIDIA_VISIBLE_DEVICES={OTHER}", f"CUDA_VISIBLE_DEVICES={OTHER}"],
                          device_requests=ALL_GPUS)),
        "transcoder": ({"name": "transcoder", "service": "transcoder", "state": "running", "health": None,
                        "status": "Up 1 hour", "image": "x"},
                       _raw(env=[f"CUDA_VISIBLE_DEVICES={LEASED}"], device_requests=ALL_GPUS)),
    },
    "adguard": {
        "adguard-adguardhome-1": ({"name": "adguard-adguardhome-1", "service": "adguardhome", "state": "running",
                                   "health": None, "status": "Up 5 days", "image": "adguard/adguardhome"},
                                  _raw()),
    },
    "min-max": {
        "min-max-web-dev-1": ({"name": "min-max-web-dev-1", "service": "web-dev", "state": "running",
                               "health": None, "status": "Up", "image": "node"}, _raw()),
    },
}


# --------------------------------------------------------------------------- #
# config: ordo.yaml `managed_projects:`
# --------------------------------------------------------------------------- #

def _source(**extra):
    return Source.from_dict({"hardware": HARDWARE, "model": "auto", "plugins": "auto", **extra})


def test_managed_projects_default_to_none():
    assert _source().managed_projects == []


def test_managed_projects_are_read_from_ordo_yaml():
    assert _source(managed_projects=["nas-stack", "azerothcore", "adguard"]).managed_projects == [
        "nas-stack", "azerothcore", "adguard"]


@pytest.mark.parametrize("value, why", [
    ("nas-stack", "list"),
    (["nas-stack", "nas-stack"], "twice"),
    (["Nas_Stack"], "compose project name"),
    (["../etc"], "compose project name"),
    ([""], "compose project name"),
    ([7], "compose project name"),
    (["ordo"], "Ordo's own project"),
])
def test_a_bad_managed_project_list_is_refused(value, why):
    with pytest.raises(ValueError, match=why):
        _source(managed_projects=value)


# --------------------------------------------------------------------------- #
# the GPU guard (pure): may a restart of this container touch the leased card?
#
# One rule (managed.gpu_refusal): refuse when the container's device requests or its
# NVIDIA_VISIBLE_DEVICES expose the leased card (by uuid, by index, or as "all"/a bare count),
# UNLESS CUDA_VISIBLE_DEVICES pins it to other cards only: on Docker Desktop/WSL2 that is the one
# pin that isolates. Indexes are resolved through the host's GPU inventory (nvidia-smi order).
# --------------------------------------------------------------------------- #

INVENTORY = {"0": OTHER, "1": LEASED}      # this host: 0 = GTX 1070, 1 = RTX 5090
JELLYFIN = _raw(env=[f"NVIDIA_VISIBLE_DEVICES={OTHER}", f"CUDA_VISIBLE_DEVICES={OTHER}"], device_requests=ALL_GPUS)
CREW_LLM = _raw(env=["NVIDIA_VISIBLE_DEVICES=all", f"CUDA_VISIBLE_DEVICES={OTHER}"], device_requests=ALL_GPUS)
ALL_GPU = _raw(device_requests=ALL_GPUS)


@pytest.mark.parametrize("raw", [
    _raw(),                                                                          # no GPU at all
    JELLYFIN,                                                                        # live jellyfin
    CREW_LLM,                                                                        # live azerothcore-crew-llm-1
    _raw(env=["CUDA_DEVICE_ORDER=PCI_BUS_ID", "CUDA_VISIBLE_DEVICES=0"],             # CUDA index, PCI order
         device_requests=ALL_GPUS),
    _raw(device_requests=[{"Driver": "nvidia", "Count": 0, "DeviceIDs": [OTHER.lower()]}]),
    _raw(device_requests=[{"Driver": "nvidia", "Count": 0, "DeviceIDs": ["0"]}]),   # the 1070 by index
    _raw(env=[f"NVIDIA_VISIBLE_DEVICES={OTHER}"], runtime="nvidia"),
], ids=["no-gpu", "jellyfin", "crew-llm", "cuda-index-0", "device-uuid", "device-index-0", "runtime-pinned"])
def test_a_container_off_the_leased_card_may_restart(raw):
    assert managed.gpu_refusal(raw, LEASED, INVENTORY) is None


@pytest.mark.parametrize("raw", [
    ALL_GPU,                                                                         # docker picks: any card
    _raw(env=["NVIDIA_VISIBLE_DEVICES=all"], device_requests=ALL_GPUS),
    _raw(env=[f"CUDA_VISIBLE_DEVICES={LEASED}"], device_requests=ALL_GPUS),
    _raw(env=[f"CUDA_VISIBLE_DEVICES={OTHER},{LEASED}"], device_requests=ALL_GPUS),
    _raw(env=["CUDA_VISIBLE_DEVICES=1"], device_requests=ALL_GPUS),                  # the 5090 by index
    _raw(env=["CUDA_VISIBLE_DEVICES=all"], device_requests=ALL_GPUS),
    _raw(device_requests=[{"Driver": "nvidia", "Count": 1, "DeviceIDs": None}]),
    _raw(device_requests=[{"Driver": "nvidia", "Count": 0, "DeviceIDs": ["1"]}]),   # the 5090 by index
    _raw(device_requests=[{"Driver": "nvidia", "Count": 0, "DeviceIDs": ["7"]}]),   # an index no card has
    _raw(env=[f"NVIDIA_VISIBLE_DEVICES={OTHER}"],                                   # env says 1070, request adds 5090
         device_requests=[{"Driver": "nvidia", "Count": 0, "DeviceIDs": [LEASED]}]),
    _raw(runtime="nvidia"),                                                          # nvidia runtime, no pin
    # Security review of #302:
    # F2: CUDA accepts a uuid PREFIX; a short one can name the 5090 and must never clear.
    _raw(env=["CUDA_VISIBLE_DEVICES=GPU-97fe65ee"], device_requests=ALL_GPUS),
    _raw(env=["CUDA_VISIBLE_DEVICES=GPU-2"], device_requests=ALL_GPUS),
    # F2: a full-length uuid no card on this host has proves nothing either.
    _raw(env=["CUDA_VISIBLE_DEVICES=GPU-00000000-0000-0000-0000-000000000000"], device_requests=ALL_GPUS),
    # F3: CUDA's default order is fastest first, so CUDA's 0 is the 5090 here.
    _raw(env=["CUDA_VISIBLE_DEVICES=0"], device_requests=ALL_GPUS),
    _raw(env=["CUDA_DEVICE_ORDER=FASTEST_FIRST", "CUDA_VISIBLE_DEVICES=0"], device_requests=ALL_GPUS),
    # F4: CDI requests (`--device nvidia.com/gpu=all`) and unknown drivers are GPU requests.
    _raw(device_requests=[{"Driver": "cdi", "Count": 0, "DeviceIDs": ["nvidia.com/gpu=all"], "Capabilities": None}]),
    _raw(device_requests=[{"Driver": "cdi", "Count": 0, "DeviceIDs": ["nvidia.com/gpu=1"], "Capabilities": None}]),
    _raw(device_requests=[{"Driver": "", "Count": 0, "DeviceIDs": ["nvidia.com/gpu=0"], "Capabilities": None}]),
    _raw(device_requests=[{"Driver": "some-vendor", "Count": -1, "DeviceIDs": None, "Capabilities": None}]),
    # F4: a raw GPU device node (`--device /dev/dxg` is the WSL2 GPU) with no CUDA pin.
    {"Config": {"Env": []}, "HostConfig": {"Devices": [{"PathOnHost": "/dev/dxg", "PathInContainer": "/dev/dxg"}]}},
    {"Config": {"Env": []}, "HostConfig": {"Devices": [{"PathOnHost": "/dev/nvidia1", "PathInContainer": "/dev/nvidia1"}]}},
], ids=["all-gpu", "nvidia-all", "cuda-leased", "cuda-both", "cuda-index-1", "cuda-all", "count-1",
        "device-index-1", "device-index-unknown", "env-and-request", "runtime-unpinned",
        "cuda-uuid-prefix", "cuda-uuid-short", "cuda-uuid-unknown", "cuda-index-default-order",
        "cuda-index-fastest-first", "cdi-all", "cdi-index", "cdi-no-driver", "unknown-driver",
        "dev-dxg", "dev-nvidia"])
def test_a_container_that_could_reach_the_leased_card_is_refused(raw):
    assert "leased" in managed.gpu_refusal(raw, LEASED, INVENTORY)


def test_without_an_inventory_no_pin_clears_a_container():
    """No nvidia-smi answer: neither an index nor a uuid can be matched to a card on this host,
    so no CUDA pin counts (F2: only a full uuid that matches the inventory clears)."""
    raw = _raw(env=["CUDA_DEVICE_ORDER=PCI_BUS_ID", "CUDA_VISIBLE_DEVICES=0"], device_requests=ALL_GPUS)
    assert managed.gpu_refusal(raw, LEASED, {})
    assert managed.gpu_refusal(CREW_LLM, LEASED, {})


def test_a_cdi_request_for_another_card_by_full_uuid_is_allowed():
    raw = _raw(device_requests=[{"Driver": "cdi", "Count": 0, "DeviceIDs": [f"nvidia.com/gpu={OTHER}"],
                                 "Capabilities": None}])
    assert managed.gpu_refusal(raw, LEASED, INVENTORY) is None


def test_a_gpu_device_node_with_a_cuda_pin_elsewhere_is_allowed():
    raw = {"Config": {"Env": [f"CUDA_VISIBLE_DEVICES={OTHER}"]},
           "HostConfig": {"Devices": [{"PathOnHost": "/dev/dxg", "PathInContainer": "/dev/dxg"}]}}
    assert managed.gpu_refusal(raw, LEASED, INVENTORY) is None


def test_an_unknown_leased_card_refuses_every_gpu_container():
    """Without the arbitrated card's uuid nothing can be proven off it: fail closed."""
    assert managed.gpu_refusal(JELLYFIN, None, INVENTORY)
    assert managed.gpu_refusal(_raw(), None, INVENTORY) is None


def test_the_refusal_never_quotes_the_environment():
    raw = _raw(env=["NVIDIA_VISIBLE_DEVICES=all", "API_KEY=sekrit"], device_requests=ALL_GPUS)
    assert "sekrit" not in managed.gpu_refusal(raw, LEASED, INVENTORY)


# --------------------------------------------------------------------------- #
# the restart budget (pure)
# --------------------------------------------------------------------------- #

def test_a_reservation_is_atomic_across_threads():
    """F1 (review of #302): check and record under one lock, so N racing callers get exactly
    `limit` slots, never N."""
    import threading
    budget = managed.RestartBudget(limit=3, window_seconds=3600)
    barrier = threading.Barrier(12)
    granted = []

    def worker():
        barrier.wait()
        granted.append(budget.reserve("nas-stack/janitorr") is None)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert granted.count(True) == 3


def test_a_refunded_reservation_frees_its_slot():
    budget = managed.RestartBudget(limit=1, window_seconds=3600)
    assert budget.reserve("k") is None
    assert budget.reserve("k") is not None
    budget.refund("k")
    assert budget.reserve("k") is None


def test_three_restarts_per_container_per_hour_then_refused():
    clock = [1000.0]
    budget = managed.RestartBudget(limit=3, window_seconds=3600, clock=lambda: clock[0])
    for _ in range(3):
        assert budget.reserve("nas-stack/janitorr") is None
    wait = budget.reserve("nas-stack/janitorr")
    assert wait is not None and 0 < wait <= 3600
    assert budget.reserve("nas-stack/jellyfin") is None         # per container
    clock[0] += 3601
    assert budget.reserve("nas-stack/janitorr") is None         # a sliding hour


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #

@pytest.fixture
def plane(tmp_path, monkeypatch):
    audit_path = tmp_path / "data" / "audit.log"
    monkeypatch.setattr("ordo.control.api.AUDIT_LOG_PATH", audit_path)
    # The host inventory comes from nvidia-smi; a test never reads the machine it runs on.
    monkeypatch.setattr(ControlPlane, "_gpu_indexes", staticmethod(lambda: INVENTORY))
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump({"hardware": HARDWARE, "model": "auto", "plugins": "auto",
                                   "managed_projects": ["nas-stack", "adguard"]}))
    scheduler = Scheduler(32)
    backend = MockBackend()
    backend.foreign = FOREIGN
    cp = ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out", scheduler=scheduler,
                      broker=Broker(scheduler, backend))
    return cp, audit_path


@pytest.fixture
def client(plane):
    cp, _ = plane
    return TestClient(cp.app(auth_token=ADMIN_TOKEN, scoped=[principals.hermes(lambda: HERMES_TOKEN)]),
                      raise_server_exceptions=False)


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _restart(client, project, name, confirm=True):
    return client.post(f"/projects/{project}/containers/{name}/restart", json={"confirm": confirm}, headers=HERMES)


def test_the_overview_lists_only_managed_projects(client):
    r = client.get("/projects", headers=HERMES)
    assert r.status_code == 200
    projects = {p["project"]: p for p in r.json()["projects"]}
    assert set(projects) == {"nas-stack", "adguard"}
    names = {c["name"] for c in projects["nas-stack"]["containers"]}
    assert names == {"janitorr", "jellyfin", "transcoder"}


def test_a_project_row_carries_no_environment(client):
    r = client.get("/projects/nas-stack/containers", headers=HERMES)
    assert r.status_code == 200
    assert "sekrit" not in r.text
    row = next(c for c in r.json()["containers"] if c["name"] == "janitorr")
    assert set(row) == {"name", "service", "state", "health", "status", "image"}


@pytest.mark.parametrize("path", ["/projects/min-max/containers", "/projects/ordo/containers",
                                  "/projects/min-max/containers/min-max-web-dev-1/logs"])
def test_an_unlisted_project_is_404(client, path):
    assert client.get(path, headers=HERMES).status_code == 404


def test_logs_are_tailed_and_capped(plane, client):
    cp, _ = plane
    assert client.get("/projects/nas-stack/containers/janitorr/logs?tail=50", headers=HERMES).status_code == 200
    assert client.get("/projects/nas-stack/containers/janitorr/logs?tail=999999", headers=HERMES).status_code == 200
    assert cp.broker.backend.foreign_log_requests == [("nas-stack", "janitorr", 50), ("nas-stack", "janitorr", 2000)]
    assert client.get("/projects/nas-stack/containers/janitorr/logs?tail=abc", headers=HERMES).status_code == 422


def test_a_container_of_another_project_is_404_even_under_a_listed_one(plane, client):
    """The container must carry the listed project's label: a name is never trusted alone."""
    cp, _ = plane
    assert _restart(client, "nas-stack", "ordo-llamacpp-1").status_code == 404
    assert _restart(client, "nas-stack", "min-max-web-dev-1").status_code == 404
    assert cp.broker.backend.foreign_restarts == []


@pytest.mark.parametrize("confirm", [False, "true", 1, None])
def test_a_restart_needs_json_true(plane, client, confirm):
    cp, _ = plane
    assert _restart(client, "nas-stack", "janitorr", confirm=confirm).status_code == 400
    assert cp.broker.backend.foreign_restarts == []


def test_a_confirmed_restart_restarts_the_container(plane, client):
    cp, _ = plane
    r = _restart(client, "nas-stack", "janitorr")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "project": "nas-stack", "container": "janitorr", "action": "restarted"}
    assert cp.broker.backend.foreign_restarts == [("nas-stack", "janitorr")]


def test_a_gpu_container_off_the_leased_card_restarts(plane, client):
    cp, _ = plane
    assert _restart(client, "nas-stack", "jellyfin").status_code == 200
    assert cp.broker.backend.foreign_restarts == [("nas-stack", "jellyfin")]


def test_a_container_on_the_leased_card_is_refused_with_409(plane, client):
    cp, _ = plane
    r = _restart(client, "nas-stack", "transcoder")
    assert r.status_code == 409
    assert "leased" in r.json()["error"]
    assert cp.broker.backend.foreign_restarts == []


def test_the_fourth_restart_in_an_hour_is_429(plane, client):
    cp, _ = plane
    for _ in range(3):
        assert _restart(client, "nas-stack", "janitorr").status_code == 200
    r = _restart(client, "nas-stack", "janitorr")
    assert r.status_code == 429
    assert r.json()["retry_after_seconds"] > 0
    assert len(cp.broker.backend.foreign_restarts) == 3
    assert _restart(client, "adguard", "adguard-adguardhome-1").status_code == 200


def test_parallel_restarts_cannot_beat_the_budget(plane):
    """F1 (review of #302): six parallel confirmed restarts gave six 200s. Now exactly three."""
    import threading
    import time
    cp, _ = plane
    backend = cp.broker.backend
    original = backend.foreign_restart

    def slow_restart(project, name):
        time.sleep(0.2)                     # every caller is past the check before any records
        original(project, name)

    backend.foreign_restart = slow_restart
    barrier = threading.Barrier(6)
    statuses = []

    def worker():
        barrier.wait()
        statuses.append(cp.route("POST", "/projects/nas-stack/containers/janitorr/restart", {"confirm": True}, {})[0])

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200, 200, 200, 429, 429, 429]
    assert len(backend.foreign_restarts) == 3


def test_a_failed_restart_refunds_its_slot(plane, client):
    cp, _ = plane

    def broken(project, name):
        raise RuntimeError("docker restart failed")

    original = cp.broker.backend.foreign_restart
    cp.broker.backend.foreign_restart = broken
    for _ in range(3):
        assert _restart(client, "nas-stack", "janitorr").status_code == 500
    cp.broker.backend.foreign_restart = original
    assert _restart(client, "nas-stack", "janitorr").status_code == 200


def test_a_refused_restart_does_not_spend_the_budget(plane, client):
    for _ in range(3):
        assert _restart(client, "nas-stack", "janitorr", confirm=False).status_code == 400
    assert _restart(client, "nas-stack", "janitorr").status_code == 200


def test_every_restart_is_audited_under_the_principal(plane, client):
    _, audit_path = plane
    _restart(client, "nas-stack", "janitorr")
    _restart(client, "nas-stack", "transcoder")
    ok, refused = _records(audit_path)
    assert (ok["principal"], ok["caller"], ok["action"], ok["target"], ok["status"]) == (
        "hermes", "hermes:discord", "project.restart", "nas-stack/janitorr", 200)
    assert (refused["action"], refused["target"], refused["status"]) == ("project.restart", "nas-stack/transcoder", 409)


def test_a_foreign_log_read_is_audited_under_the_principal(plane, client):
    """F6 (review of #302): reading another project's logs is recorded, like a write."""
    _, audit_path = plane
    client.get("/projects/nas-stack/containers/janitorr/logs?tail=10", headers=HERMES)
    [rec] = _records(audit_path)
    assert (rec["principal"], rec["caller"], rec["method"], rec["action"], rec["target"], rec["status"]) == (
        "hermes", "hermes:discord", "GET", "project.logs", "nas-stack/janitorr", 200)
    assert rec["path"] == "/projects/nas-stack/containers/janitorr/logs"


def test_other_managed_reads_are_not_audited(plane, client):
    _, audit_path = plane
    client.get("/projects", headers=HERMES)
    client.get("/projects/nas-stack/containers", headers=HERMES)
    assert _records(audit_path) == []


def test_hermes_has_status_logs_and_restart_only():
    hermes = principals.hermes(lambda: HERMES_TOKEN)
    assert hermes.allows("GET", "/projects")
    assert hermes.allows("GET", "/projects/nas-stack/containers")
    assert hermes.allows("GET", "/projects/nas-stack/containers/janitorr/logs")
    assert hermes.allows("POST", "/projects/nas-stack/containers/janitorr/restart")
    for method, path in [("POST", "/projects/nas-stack/containers/janitorr/stop"),
                         ("POST", "/projects/nas-stack/containers/janitorr/start"),
                         ("GET", "/projects/nas-stack/containers/janitorr"),
                         ("POST", "/projects/nas-stack/containers/a/b/restart")]:
        assert not hermes.allows(method, path)


def test_no_verb_but_status_logs_and_restart_exists(plane):
    cp, _ = plane
    for method, path in [("POST", "/projects/nas-stack/containers/janitorr/stop"),
                         ("POST", "/projects/nas-stack/containers/janitorr/start"),
                         ("DELETE", "/projects/nas-stack/containers/janitorr")]:
        assert cp.route(method, path, {"confirm": True}, {})[0] == 404


# --------------------------------------------------------------------------- #
# F5 (review of #302): a container NAMED after another container's full ID
# --------------------------------------------------------------------------- #

VICTIM_ID = "a" * 64
DECOY_ID = "b" * 64


class FakeDocker:
    """`docker` as the DockerBackend sees it: nas-stack holds one container whose NAME is the
    victim's full ID. Docker resolves a name-or-ID argument by ID first, so acting on the name
    would act on the victim (another project's container)."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        import subprocess
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            out = f"decoy\t{VICTIM_ID}\trunning\tUp 1 hour\talpine\t{DECOY_ID}\n"
        elif argv[:2] == ["docker", "inspect"]:
            out = json.dumps([{"Id": argv[-1], "Config": {"Env": []}, "HostConfig": {}}])
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture
def docker_backend(monkeypatch):
    from ordo.control import broker as broker_module
    fake = FakeDocker()
    monkeypatch.setattr(broker_module.subprocess, "run", fake)
    return broker_module.DockerBackend(project="ordo"), fake


@pytest.mark.parametrize("verb", ["foreign_restart", "foreign_logs", "foreign_inspect"])
def test_the_backend_acts_on_the_labelled_containers_id_not_its_name(docker_backend, verb):
    backend, fake = docker_backend
    args = ("nas-stack", VICTIM_ID, 10) if verb == "foreign_logs" else ("nas-stack", VICTIM_ID)
    getattr(backend, verb)(*args)
    acted = [argv for argv in fake.calls if argv[:2] != ["docker", "ps"]]
    assert len(acted) == 1
    assert acted[0][-1] == DECOY_ID
    assert VICTIM_ID not in acted[0]


def test_the_ps_query_is_label_filtered_and_untruncated(docker_backend):
    backend, fake = docker_backend
    backend.foreign_restart("nas-stack", VICTIM_ID)
    ps = next(argv for argv in fake.calls if argv[:2] == ["docker", "ps"])
    assert "label=com.docker.compose.project=nas-stack" in ps
    assert "--no-trunc" in ps
