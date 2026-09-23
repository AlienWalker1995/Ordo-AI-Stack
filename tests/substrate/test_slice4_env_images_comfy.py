"""The last four routes the V1 dashboard calls, ported to ops-controller (slice 4).

`/env/set`, `/images/pull` and `/comfyui/install-node-requirements` are live verbs; `/gpu/assign`
and `/registry/models/{id}/assign-gpu` are the two that ops-api answers 410 GONE, and the 410 has
to survive the port or the dashboard silently starts believing a runtime GPU pin took effect.

Every test drives `route()` rather than the FastAPI app: the transport is a thin binding over that
pure function, and this is where the behaviour lives.
"""
from pathlib import Path

import pytest
import yaml

from ordo.broker import Broker, MockBackend
from ordo.catalog import Catalog
from ordo.control import ControlPlane
from ordo.plugins import PluginRegistry
from ordo.scheduler import Scheduler

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")


@pytest.fixture
def cp(tmp_path, monkeypatch):
    """A control plane whose .env, audit log and custom_nodes dir all live under tmp_path."""
    env_path = tmp_path / ".env"
    env_path.write_text("LLAMACPP_CTX_SIZE=131072\nUNRELATED=keep-me\n", encoding="utf-8")
    monkeypatch.setattr("ordo.control.OPS_ENV_PATH", env_path)
    monkeypatch.setattr("ordo.control.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr("ordo.control.COMFYUI_CUSTOM_NODES_DIR", tmp_path / "custom_nodes")

    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(
        {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto", "plugins": "auto"}
    ))
    scheduler = Scheduler(32)
    backend = MockBackend()
    plane = ControlPlane(
        src, CATALOG, REGISTRY, tmp_path / "out",
        scheduler=scheduler, broker=Broker(scheduler, backend),
    )
    return plane, backend, tmp_path


# --- POST /env/set ---

def test_env_set_rewrites_the_key_in_place_and_leaves_the_rest_alone(cp):
    plane, _, tmp_path = cp
    code, body = plane.route("POST", "/env/set", {"key": "LLAMACPP_CTX_SIZE", "value": "65536", "confirm": True})
    assert code == 200
    assert body == {"ok": True, "key": "LLAMACPP_CTX_SIZE"}
    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "LLAMACPP_CTX_SIZE=65536" in content
    assert "UNRELATED=keep-me" in content


def test_env_set_appends_a_key_that_is_not_there_yet(cp):
    plane, _, tmp_path = cp
    code, _ = plane.route("POST", "/env/set", {"key": "DEFAULT_MODEL", "value": "local-chat", "confirm": True})
    assert code == 200
    assert "DEFAULT_MODEL=local-chat" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_env_set_without_confirm_changes_nothing(cp):
    plane, _, tmp_path = cp
    before = (tmp_path / ".env").read_text(encoding="utf-8")
    code, body = plane.route("POST", "/env/set", {"key": "LLAMACPP_CTX_SIZE", "value": "8"})
    assert code == 400
    assert "confirm" in body["error"]
    assert (tmp_path / ".env").read_text(encoding="utf-8") == before


def test_env_set_refuses_a_key_outside_the_allowlist(cp):
    plane, _, _ = cp
    code, body = plane.route("POST", "/env/set", {"key": "PATH", "value": "/evil", "confirm": True})
    assert code == 400
    assert "allowlist" in body["error"]


def test_env_set_refuses_a_newline_in_the_value(cp):
    """A newline would inject a second assignment into .env, which every compose call then reads."""
    plane, _, _ = cp
    code, body = plane.route(
        "POST", "/env/set", {"key": "DEFAULT_MODEL", "value": "a\nCADDY_BIND=0.0.0.0", "confirm": True},
    )
    assert code == 400
    assert "newline" in body["error"].lower()


def test_env_set_refuses_shell_metacharacters_in_llamacpp_extra_args(cp):
    """This value is word-split by the run script, so it reaches a shell."""
    plane, _, _ = cp
    code, body = plane.route(
        "POST", "/env/set", {"key": "LLAMACPP_EXTRA_ARGS", "value": "--foo; rm -rf /", "confirm": True},
    )
    assert code == 400
    assert "LLAMACPP_EXTRA_ARGS" in body["error"]


def test_env_set_writes_an_audit_record(cp):
    plane, _, tmp_path = cp
    plane.route("POST", "/env/set", {"key": "DEFAULT_MODEL", "value": "local-chat", "confirm": True})
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert '"action":"env_set"' in lines[0]
    assert '"target":"DEFAULT_MODEL"' in lines[0]


# --- POST /images/pull ---

def test_images_pull_pulls_each_named_service(cp):
    plane, backend, _ = cp
    code, body = plane.route("POST", "/images/pull", {"services": ["llamacpp", "comfyui"]})
    assert code == 200
    assert body == {"ok": True, "services": ["llamacpp", "comfyui"]}
    assert backend.pulled == ["llamacpp", "comfyui"]


def test_images_pull_with_no_services_is_a_400(cp):
    plane, backend, _ = cp
    code, body = plane.route("POST", "/images/pull", {"services": []})
    assert code == 400
    assert backend.pulled == []


def test_images_pull_reports_the_failures_and_still_names_what_succeeded(cp):
    plane, backend, _ = cp

    def only_llamacpp_pulls(service):
        if service != "llamacpp":
            raise RuntimeError("manifest unknown")
        backend.pulled.append(service)

    backend.pull_image = only_llamacpp_pulls
    code, body = plane.route("POST", "/images/pull", {"services": ["llamacpp", "nope"]})
    assert code == 500
    assert "manifest unknown" in body["error"]
    assert body["services"] == ["llamacpp"]


# --- POST /comfyui/install-node-requirements ---

def _make_node_pack(tmp_path, name="ComfyUI-Thing"):
    pack = tmp_path / "custom_nodes" / name
    pack.mkdir(parents=True)
    (pack / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    return name


def test_install_node_requirements_runs_pip_inside_the_comfyui_container(cp):
    plane, backend, tmp_path = cp
    name = _make_node_pack(tmp_path)
    backend.exec_result = (0, "Successfully installed numpy")
    code, body = plane.route(
        "POST", "/comfyui/install-node-requirements", {"node_path": name, "confirm": True},
    )
    assert code == 200
    assert body["ok"] is True
    container, command = backend.execs[0]
    assert container == "ordo-comfyui-1"
    assert command == [
        "python3", "-m", "pip", "install", "-r",
        f"/root/ComfyUI/custom_nodes/{name}/requirements.txt",
    ]


def test_install_node_requirements_surfaces_a_pip_failure_as_500(cp):
    plane, backend, tmp_path = cp
    name = _make_node_pack(tmp_path)
    backend.exec_result = (1, "ERROR: no matching distribution")
    code, body = plane.route(
        "POST", "/comfyui/install-node-requirements", {"node_path": name, "confirm": True},
    )
    assert code == 500
    assert body["ok"] is False
    assert body["exit_code"] == 1


def test_install_node_requirements_without_confirm_runs_nothing(cp):
    plane, backend, tmp_path = cp
    name = _make_node_pack(tmp_path)
    code, _ = plane.route("POST", "/comfyui/install-node-requirements", {"node_path": name})
    assert code == 400
    assert backend.execs == []


def test_install_node_requirements_is_404_when_the_pack_has_no_requirements(cp):
    plane, backend, tmp_path = cp
    (tmp_path / "custom_nodes" / "Bare").mkdir(parents=True)
    code, body = plane.route(
        "POST", "/comfyui/install-node-requirements", {"node_path": "Bare", "confirm": True},
    )
    assert code == 404
    assert "requirements.txt" in body["error"]
    assert backend.execs == []


@pytest.mark.parametrize("node_path", ["../../etc", "a/../../b", "", "has space", "x" * 300])
def test_install_node_requirements_refuses_a_path_that_escapes_custom_nodes(cp, node_path):
    plane, backend, _ = cp
    code, _ = plane.route(
        "POST", "/comfyui/install-node-requirements", {"node_path": node_path, "confirm": True},
    )
    assert code == 400
    assert backend.execs == []


def test_install_node_requirements_normalises_a_leading_slash_rather_than_rejecting_it(cp):
    """A leading slash is stripped, so the path stays under custom_nodes and simply does not
    exist. This is the behaviour ops-api had; it is safe because the traversal check runs after."""
    plane, backend, _ = cp
    code, _ = plane.route(
        "POST", "/comfyui/install-node-requirements", {"node_path": "/absolute", "confirm": True},
    )
    assert code == 404
    assert backend.execs == []


def test_install_node_requirements_is_503_when_comfyui_is_not_running(cp):
    plane, backend, tmp_path = cp
    name = _make_node_pack(tmp_path)

    def not_running(container, command):
        raise FileNotFoundError(container)

    backend.exec_in = not_running
    code, body = plane.route(
        "POST", "/comfyui/install-node-requirements", {"node_path": name, "confirm": True},
    )
    assert code == 503
    assert "start comfyui" in body["error"]


# --- the two 410s ---

def test_gpu_assign_is_gone_not_a_silent_success(cp):
    """V1 answered {"ok": true} and changed nothing. A 410 is the honest answer."""
    plane, _, _ = cp
    code, body = plane.route("POST", "/gpu/assign", {"service": "llamacpp", "gpu": "GPU-abc"})
    assert code == 410
    assert "render" in body["error"]


def test_registry_assign_gpu_is_gone_too(cp):
    plane, _, _ = cp
    code, body = plane.route("POST", "/registry/models/qwen3.8-27b/assign-gpu", {"gpu": "GPU-abc"})
    assert code == 410
    assert "render" in body["error"]


# --- .env is the file whose mtime recreates the whole stack, so writes must be surgical ---

def test_env_set_preserves_crlf_line_endings(cp):
    """The renderer runs on Windows and writes CRLF; the controller runs in a Linux container.

    Live defect 2026-09-23: read_text/write_text translated newlines, so setting one key rewrote
    all 55 lines as LF and the next `ordo render` flipped them back. Nothing in the values changed,
    but every byte did, on the file whose mtime marks ~41 containers for recreation.
    """
    plane, _, tmp_path = cp
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"LLAMACPP_CTX_SIZE=131072\r\nUNRELATED=keep-me\r\n")
    code, _ = plane.route(
        "POST", "/env/set", {"key": "LLAMACPP_CTX_SIZE", "value": "65536", "confirm": True},
    )
    assert code == 200
    assert env_path.read_bytes() == b"LLAMACPP_CTX_SIZE=65536\r\nUNRELATED=keep-me\r\n"


def test_env_set_with_an_unchanged_value_is_a_byte_for_byte_no_op(cp):
    plane, _, tmp_path = cp
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"LLAMACPP_CTX_SIZE=131072\r\nUNRELATED=keep-me\r\n")
    before = env_path.read_bytes()
    plane.route("POST", "/env/set", {"key": "LLAMACPP_CTX_SIZE", "value": "131072", "confirm": True})
    assert env_path.read_bytes() == before


def test_env_set_appends_using_the_line_ending_the_file_already_uses(cp):
    plane, _, tmp_path = cp
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"UNRELATED=keep-me\r\n")
    plane.route("POST", "/env/set", {"key": "DEFAULT_MODEL", "value": "local-chat", "confirm": True})
    assert env_path.read_bytes() == b"UNRELATED=keep-me\r\nDEFAULT_MODEL=local-chat\r\n"
