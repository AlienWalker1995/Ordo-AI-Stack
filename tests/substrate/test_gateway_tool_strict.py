"""A chat model's catalog `gateway: {drop_tool_strict: true}`: model-gateway removes `strict` from every
tool definition before the request reaches that model's server.

Some OpenAI-compatible servers (NInfer) refuse a tool marked `"strict": true` with a 400, and clients
such as the evals harness, Open WebUI and external MCP clients send it. The gateway owns
client-to-backend translation, so the flag travels catalog -> render (.env GATEWAY_DROP_TOOL_STRICT,
declared for model-gateway only) -> entrypoint.sh (LiteLLM `additional_drop_params:
["tools[*].function.strict"]` on the GPU chat entries) -> LiteLLM, which deletes the nested field in
get_optional_params (litellm/utils.py, litellm_core_utils/dot_notation_indexing.py in 1.100.1).

The render, catalog and entrypoint halves run everywhere. The `docker` half builds the real gateway
image, puts a stub OpenAI server on the `llamacpp` alias and checks what the stub received.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

from ordo.render.catalog import Catalog, Model
from ordo.render.config import Source
from ordo.render.engine import render

ROOT = Path(__file__).resolve().parents[2]
GATEWAY_DIR = ROOT / "services" / "model-gateway"
ENTRYPOINT = GATEWAY_DIR / "entrypoint.sh"
TEMPLATE = GATEWAY_DIR / "litellm_config.yaml"
PROFILE_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32, "platform": "Linux"}
DROP_STRICT = ["tools[*].function.strict"]
MASTER_KEY = "sk-" + "t" * 40


def _entry(**extra) -> dict:
    base = {"id": "m", "file": "m.gguf", "source": "https://example.invalid/m.gguf", "sha256": "a" * 64,
            "requires": {"vram_gb": 10}, "tier": "high"}
    base.update(extra)
    return base


def _render(model: Model):
    return render(Source.from_dict({"hardware": PROFILE_5090, "model": model.id, "plugins": "auto"}),
                  Catalog([model]))


# --- the catalog ------------------------------------------------------------------------------------------

def test_the_flag_defaults_to_off():
    assert Model.from_dict(_entry()).gateway_drop_tool_strict is False


def test_the_flag_is_read_from_the_gateway_mapping():
    assert Model.from_dict(_entry(gateway={"drop_tool_strict": True})).gateway_drop_tool_strict is True
    assert Model.from_dict(_entry(gateway={"drop_tool_strict": False})).gateway_drop_tool_strict is False


@pytest.mark.parametrize("gateway, message", [
    ({"drop_strict": True}, "unknown gateway key"),
    ({"drop_tool_strict": "yes"}, "must be true or false"),
    (["drop_tool_strict"], "must be a mapping"),
])
def test_a_malformed_gateway_mapping_is_refused(gateway, message):
    with pytest.raises(ValueError, match=message):
        Model.from_dict(_entry(gateway=gateway))


def test_no_shipped_catalog_entry_is_malformed():
    # Loading parses every entry, so a typo in a `gateway:` mapping fails here, not at render time.
    Catalog.load(ROOT / "catalog" / "models.yaml")


# --- the render ------------------------------------------------------------------------------------------

def test_a_model_with_the_flag_renders_the_key_for_model_gateway_only():
    rc = _render(Model.from_dict(_entry(gateway={"drop_tool_strict": True})))
    assert rc.env["GATEWAY_DROP_TOOL_STRICT"] == "true"
    services = rc.compose_dict()["services"]
    readers = sorted(name for name, svc in services.items()
                     if "GATEWAY_DROP_TOOL_STRICT" in (svc.get("environment") or {}))
    assert readers == ["model-gateway"]
    assert services["model-gateway"]["environment"]["GATEWAY_DROP_TOOL_STRICT"].startswith(
        "${GATEWAY_DROP_TOOL_STRICT?")


def test_a_model_without_the_flag_renders_no_key():
    rc = _render(Model.from_dict(_entry()))
    assert "GATEWAY_DROP_TOOL_STRICT" not in rc.env
    assert "GATEWAY_DROP_TOOL_STRICT" not in rc.compose_dict()["services"]["model-gateway"].get("environment", {})


# --- the entrypoint: the real substitution against the real template ------------------------------------

def _sh() -> str:
    path = shutil.which("sh") or shutil.which("bash")
    if not path:
        pytest.skip("POSIX sh not available on PATH")
    return path


def _run_substitution(tmp_path: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    out_path = tmp_path / "config.yaml"
    script = ENTRYPOINT.read_text(encoding="utf-8")
    script = script.replace("/app/config.template.yaml", TEMPLATE.resolve().as_posix())
    script = script.replace("/tmp/config.yaml", out_path.as_posix())
    script = script.replace("/app/secret-env.sh", (GATEWAY_DIR / "secret-env.sh").resolve().as_posix())
    marker = "cp /app/throughput_callback.py"
    assert marker in script, "entrypoint.sh shape changed: update this test's truncation point"
    script = script[: script.index(marker)]
    env = {k: v for k, v in os.environ.items() if k not in ("LITELLM_MASTER_KEY", "GATEWAY_DROP_TOOL_STRICT")}
    env.update({"LITELLM_MASTER_KEY": MASTER_KEY, "LLAMACPP_CTX_SIZE": "131072", "LLAMACPP_CPU_CTX": "131072",
                "LLAMACPP_MODEL": "NInfer-Test.gguf", **extra_env})
    return subprocess.run([_sh()], input=script, env=env, capture_output=True, text=True, timeout=10)


def _drops_by_model(tmp_path: Path, extra_env: dict[str, str]) -> dict[str, list | None]:
    result = _run_substitution(tmp_path, extra_env)
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert not re.search(r"__[A-Z_]+__", text), "unsubstituted placeholder left in the rendered config"
    config = yaml.safe_load(text)
    return {m["model_name"]: m["litellm_params"].get("additional_drop_params") for m in config["model_list"]}


def test_the_flag_drops_strict_on_the_gpu_chat_entries_only(tmp_path):
    drops = _drops_by_model(tmp_path, {"GATEWAY_DROP_TOOL_STRICT": "true"})
    assert drops["local-chat"] == DROP_STRICT
    assert drops["ninfer-test"] == DROP_STRICT          # the GPU pin alias, named from the weights
    # The CPU fallback and the embedder run stock llama.cpp, which accepts (and ignores) strict.
    others = {name: value for name, value in drops.items() if name not in ("local-chat", "ninfer-test")}
    assert others and all(value is None for value in others.values()), others


def test_without_the_flag_nothing_is_dropped(tmp_path):
    drops = _drops_by_model(tmp_path, {})
    assert drops["local-chat"] == [] and drops["ninfer-test"] == []


def test_a_malformed_flag_refuses_to_start(tmp_path):
    result = _run_substitution(tmp_path, {"GATEWAY_DROP_TOOL_STRICT": "1"})
    assert result.returncode == 1
    assert "GATEWAY_DROP_TOOL_STRICT" in result.stderr


# --- behaviour: the real gateway image in front of a stub OpenAI server ----------------------------------

# A stdlib OpenAI-compatible stub: appends every POST body to /tmp/requests.jsonl and answers a fixed
# chat completion. Runs in the gateway image itself (it has python3), so the test needs one image.
STUB_SERVER = r'''
import http.server, json
class Stub(http.server.BaseHTTPRequestHandler):
    def _reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self._reply({"object": "list", "data": [{"id": "local-chat", "object": "model"}]})
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open("/tmp/requests.jsonl", "ab") as f:
            f.write(body.replace(b"\n", b" ") + b"\n")
        self._reply({"id": "chatcmpl-stub", "object": "chat.completion", "created": 0, "model": "local-chat",
                     "choices": [{"index": 0, "finish_reason": "stop",
                                  "message": {"role": "assistant", "content": "ok"}}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Stub).serve_forever()
'''

# Runs inside the gateway container: one chat request with a strict tool, through the proxy.
CLIENT = r'''
import json, sys, urllib.request
model, key = sys.argv[1], sys.argv[2]
tool = {"type": "function", "function": {"name": "get_weather", "description": "Weather for a city.", "strict": True,
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"],
                       "additionalProperties": False}}}
body = json.dumps({"model": model, "messages": [{"role": "user", "content": "weather in Oslo?"}],
                   "tools": [tool], "max_tokens": 8}).encode()
req = urllib.request.Request("http://127.0.0.1:11435/v1/chat/completions", data=body, method="POST",
                             headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
print(urllib.request.urlopen(req, timeout=60).status)
'''

READY = r'''
import urllib.request
urllib.request.urlopen("http://127.0.0.1:11435/health/liveliness", timeout=5)
'''


def _docker(*args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", timeout=30, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _wait_ready(container: str, deadline_s: float = 240) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if _docker("exec", container, "python3", "-c", READY, check=False, timeout=30).returncode == 0:
            return
        if _docker("inspect", "-f", "{{.State.Running}}", container, check=False).stdout.strip() != "true":
            break
        time.sleep(2)
    logs = _docker("logs", "--tail", "60", container, check=False)
    raise AssertionError(f"{container} never became ready:\n{logs.stdout}\n{logs.stderr}")


@pytest.fixture(scope="module")
def gateway_pair():
    """Build the gateway image, start the stub on alias `llamacpp`, and two gateways in front of it: one
    with the flag, one without. Everything is named with a random suffix and removed afterwards."""
    if not _docker_available():
        pytest.skip("docker is not reachable")
    suffix = uuid.uuid4().hex[:10]
    image = f"ordo-test-gateway-strict:{suffix}"
    network = f"ordo-test-gw-strict-{suffix}"
    stubs = {flag: f"ordo-test-gw-stub-{flag}-{suffix}" for flag in ("on", "off")}
    gateways = {flag: f"ordo-test-gw-{flag}-{suffix}" for flag in ("on", "off")}
    networks = {flag: f"{network}-{flag}" for flag in ("on", "off")}
    try:
        _docker("build", "-q", "-t", image, GATEWAY_DIR.as_posix(), timeout=900)
        for flag in ("on", "off"):
            # One network per gateway, so each one's `llamacpp` is its own stub and the recorded
            # requests cannot mix.
            _docker("network", "create", networks[flag])
            _docker("run", "-d", "--name", stubs[flag], "--network", networks[flag], "--network-alias", "llamacpp",
                    "--entrypoint", "python3", image, "-c", STUB_SERVER)
            env = ["-e", f"LITELLM_MASTER_KEY={MASTER_KEY}", "-e", "LLAMACPP_CTX_SIZE=131072",
                   "-e", "LLAMACPP_CPU_CTX=131072", "-e", "LLAMACPP_MODEL=NInfer-Test.gguf",
                   "-e", "MCP_SERVERS_FILE=/tmp/mcp_servers.yaml"]
            if flag == "on":
                env += ["-e", "GATEWAY_DROP_TOOL_STRICT=true"]
            _docker("run", "-d", "--name", gateways[flag], "--network", networks[flag], *env,
                    "--entrypoint", "sh", image, "-c",
                    "printf 'mcp_servers: {}\\n' > /tmp/mcp_servers.yaml && exec /app/entrypoint.sh")
        for flag in ("on", "off"):
            _wait_ready(gateways[flag])
        yield {flag: (gateways[flag], stubs[flag]) for flag in ("on", "off")}
    finally:
        for name in [*gateways.values(), *stubs.values()]:
            _docker("rm", "-f", name, check=False)
        for name in networks.values():
            _docker("network", "rm", name, check=False)
        _docker("rmi", "-f", image, check=False)


def _tool_requests(stub: str) -> list[dict]:
    """The chat requests carrying tools that reached the stub (health probes carry none)."""
    text = _docker("exec", stub, "cat", "/tmp/requests.jsonl", check=False).stdout
    bodies = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [b for b in bodies if b.get("tools")]


def _send(gateway: str, model: str) -> None:
    result = _docker("exec", gateway, "python3", "-c", CLIENT, model, MASTER_KEY, check=False, timeout=90)
    assert result.returncode == 0 and result.stdout.strip() == "200", (result.stdout, result.stderr)


@pytest.mark.docker
@pytest.mark.parametrize("model", ["local-chat", "ninfer-test"])
def test_strict_tools_reach_the_backend_without_strict(gateway_pair, model):
    gateway, stub = gateway_pair["on"]
    before = len(_tool_requests(stub))
    _send(gateway, model)
    received = _tool_requests(stub)[before:]
    assert len(received) == 1, received
    function = received[0]["tools"][0]["function"]
    assert "strict" not in function, function
    # Only `strict` goes: the rest of the tool definition reaches the backend as the client sent it.
    assert function["name"] == "get_weather"
    assert function["parameters"]["required"] == ["city"]
    assert function["parameters"]["additionalProperties"] is False


@pytest.mark.docker
def test_without_the_flag_strict_reaches_the_backend(gateway_pair):
    """The control: the same request through a gateway without the flag arrives with strict intact,
    so the test above would see a strict that slipped through."""
    gateway, stub = gateway_pair["off"]
    before = len(_tool_requests(stub))
    _send(gateway, "local-chat")
    received = _tool_requests(stub)[before:]
    assert len(received) == 1, received
    assert received[0]["tools"][0]["function"]["strict"] is True
