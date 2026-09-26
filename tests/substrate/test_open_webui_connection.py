"""Open WebUI's model connection is the stack's declared one on every start.

Open WebUI keeps its settings in webui.db (`config` table). With ENABLE_PERSISTENT_CONFIG at its
default (true) it reads env only on first launch and the DB row wins forever after: a first-launch
placeholder key survived three months of manifest-declared scoped keys, LiteLLM rejected it (401), and
the chat UI listed no models. v0.11.3 has no per-key override (only oauth.* has its own switch,
ENABLE_OAUTH_PERSISTENT_CONFIG), so the manifest turns persistence off: env (the render) is the
source of truth for every setting, and the settings the operator had changed in the admin UI are
declared here instead.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ordo import cli
from ordo.host import doctor
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.open_webui_probe import OPEN_WEBUI_PROBE, open_webui_verdict
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
HARDWARE = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32}
SITE = {"BASE_PATH": "/srv/ordo", "DATA_PATH": "/srv/ordo/data"}
# The image whose PersistentConfig semantics this contract was verified against. A bump must re-check
# open_webui/models/config.py (Config.persistent_enabled_for) before this pin moves.
VERIFIED_IMAGE = "ghcr.io/open-webui/open-webui:v0.11.3"


def _render(plugins: list[str]):
    source = Source.from_dict({"hardware": HARDWARE, "model": "auto", "plugins": plugins, "site": SITE})
    rc = render(source, CATALOG, REGISTRY)
    return rc, rc.compose_dict()["services"]["open-webui"]


def _interpolate(value: str, env: dict[str, str]) -> str:
    """Compose's `${VAR}`, `${VAR:-default}` and `${VAR:+replacement}`, innermost first."""
    pattern = re.compile(r"\$\{(\w+)(?:(:-|:\+)([^${}]*))?\}")

    def one(m: re.Match) -> str:
        name, op, arg = m.group(1), m.group(2), m.group(3) or ""
        value = env.get(name, "")
        if op == ":-":
            return value or arg
        if op == ":+":
            return arg if value else ""
        return value

    while pattern.search(value):
        value = pattern.sub(one, value)
    return value


@pytest.fixture(scope="module")
def with_search():
    return _render(["rag", "open-webui", "searxng-web"])


def test_persistent_config_is_off_so_env_wins_on_every_start(with_search):
    _, svc = with_search
    assert svc["image"] == VERIFIED_IMAGE, "re-verify ENABLE_PERSISTENT_CONFIG semantics before bumping"
    assert svc["environment"]["ENABLE_PERSISTENT_CONFIG"] == "false"


def test_chat_and_rag_connections_use_the_scoped_key_against_the_gateway(with_search):
    rc, svc = with_search
    env = svc["environment"]
    assert "LITELLM_KEY_OPEN_WEBUI" in rc.required_secrets
    assert env["OPENAI_API_KEY"] == "${LITELLM_KEY_OPEN_WEBUI}"
    assert env["RAG_OPENAI_API_KEY"] == "${LITELLM_KEY_OPEN_WEBUI}"
    assert _interpolate(env["OPENAI_API_BASE_URL"], rc.env) == "http://model-gateway:11435/v1"
    assert _interpolate(env["RAG_OPENAI_API_BASE_URL"], rc.env) == "http://model-gateway:11435/v1"
    assert env["ENABLE_OLLAMA_API"] == "false"


def test_rag_embeds_through_an_alias_the_scoped_key_may_use(with_search):
    """The key is scoped to the gateway aliases; a GGUF filename is refused with 403."""
    rc, svc = with_search
    allowed = REGISTRY.get("open-webui").litellm_key["models"]
    embed_model = _interpolate(svc["environment"]["RAG_EMBEDDING_MODEL"], rc.env)
    default_model = _interpolate(svc["environment"]["DEFAULT_MODELS"], rc.env)
    assert embed_model == "local-embed" and embed_model in allowed
    assert default_model in allowed


def test_operator_settings_are_declared_not_left_in_the_db(with_search):
    rc, svc = with_search
    env = {k: _interpolate(str(v), rc.env) for k, v in svc["environment"].items()}
    assert env["ENABLE_WEB_SEARCH"] == "true"
    assert env["WEB_SEARCH_ENGINE"] == "searxng"
    assert env["SEARXNG_QUERY_URL"] == "http://searxng:8080/search"
    assert env["WEB_SEARCH_RESULT_COUNT"] == "3000"
    assert env["WEB_SEARCH_CONCURRENT_REQUESTS"] == "5"
    assert env["AUDIO_TTS_ENGINE"] == "transformers"
    assert env["ENABLE_USER_WEBHOOKS"] == "true"


def test_web_search_is_off_without_the_searxng_service():
    rc, svc = _render(["rag", "open-webui"])
    assert "SEARXNG_ENABLED" not in rc.env
    assert _interpolate(svc["environment"]["ENABLE_WEB_SEARCH"], rc.env) == ""


# --- ordo doctor: the running container's declared connection actually authenticates ---

def _probe(**overrides) -> dict:
    probe = {"persistent_config": "false", "default_model": "local-chat", "embed_model": "local-embed",
             "chat": {"status": 200, "models": ["local-chat", "local-embed"]},
             "rag": {"status": 200, "models": ["local-chat", "local-embed"]}}
    probe.update(overrides)
    return probe


def test_doctor_passes_a_healthy_open_webui():
    ok, line = open_webui_verdict(_probe())
    assert ok and line.startswith("open-webui:") and "local-chat" in line


def test_doctor_passes_when_open_webui_is_not_running():
    ok, line = open_webui_verdict(None)
    assert ok and "not running" in line


def test_doctor_flags_persistent_config_left_on():
    ok, line = open_webui_verdict(_probe(persistent_config="true"))
    assert not ok and "ENABLE_PERSISTENT_CONFIG" in line and "ordo apply --only open-webui" in line


def test_doctor_flags_a_key_the_gateway_rejects():
    ok, line = open_webui_verdict(_probe(chat={"status": 401, "models": []}))
    assert not ok and "401" in line and "chat" in line


def test_doctor_flags_a_default_model_the_key_cannot_list():
    ok, line = open_webui_verdict(_probe(default_model="gone"))
    assert not ok and "gone" in line


def test_doctor_flags_an_embedding_model_the_key_cannot_use():
    ok, line = open_webui_verdict(_probe(embed_model="nomic-embed-text-v1.5.Q4_K_M.gguf"))
    assert not ok and "nomic-embed-text-v1.5.Q4_K_M.gguf" in line


def test_doctor_flags_an_unreachable_gateway():
    ok, line = open_webui_verdict(_probe(rag={"status": 0, "error": "URLError: refused", "models": []}))
    assert not ok and "refused" in line


def test_the_probe_never_prints_the_key():
    """The key stays inside the container: the probe reads it from its own env and prints only
    status codes and model ids."""
    assert "print" in OPEN_WEBUI_PROBE
    assert "OPENAI_API_KEY" in OPEN_WEBUI_PROBE
    printed = OPEN_WEBUI_PROBE.split("print(", 1)[1]
    assert "KEY" not in printed


def test_doctor_command_fails_on_an_open_webui_problem(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "read_running_substrate_digest", lambda project: None)
    monkeypatch.setattr(doctor, "read_open_webui_probe", lambda project: _probe(chat={"status": 401, "models": []}))
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert code == 1 and "! open-webui" in out


def test_doctor_command_reports_an_unreadable_open_webui(monkeypatch, capsys):
    def unreadable(project):
        raise doctor.ContainerUnreadable("docker exec failed")

    monkeypatch.setattr(doctor, "read_running_substrate_digest", lambda project: None)
    monkeypatch.setattr(doctor, "read_open_webui_probe", unreadable)
    code = cli.main(["doctor"])
    assert code == 1 and "docker exec failed" in capsys.readouterr().out
