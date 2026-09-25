"""model-gateway tracing wiring: plugin-dependent callbacks are appended by the entrypoint, never
written into the template, and a missing credential skips the callback instead of failing boot.

The template must keep booting without the langfuse plugin (it names only throughput + prometheus),
and the renderer is the one place that decides whether `langfuse_otel` is on (GATEWAY_LANGFUSE_ENV
on the model-gateway service, only while the plugin is enabled)."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from ordo.render import compose
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "model-gateway"))

from add_callbacks import add_callbacks, main, parse_callback_names, usable_callbacks  # noqa: E402

TEMPLATE = ROOT / "services" / "model-gateway" / "litellm_config.yaml"
ENTRYPOINT = ROOT / "services" / "model-gateway" / "entrypoint.sh"
DOCKERFILE = ROOT / "services" / "model-gateway" / "Dockerfile"
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32}
BASE_CALLBACKS = ["throughput_callback.throughput_recorder_instance", "prometheus"]
LANGFUSE_ENV = {"LANGFUSE_PUBLIC_KEY": "pk-lf-x", "LANGFUSE_SECRET_KEY": "sk-lf-x",
                "LANGFUSE_OTEL_HOST": "http://langfuse-web:3000"}

CONFIG = """\
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
litellm_settings:
  callbacks: ["throughput_callback.throughput_recorder_instance", "prometheus"]
"""


def _template() -> dict:
    return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))


def _gateway_env(plugins: list[str]) -> dict[str, str]:
    rc = render(Source.from_dict({"hardware": P_5090, "plugins": plugins}), CATALOG, REGISTRY)
    return rc.compose_dict()["services"]["model-gateway"]["environment"]


# ── the template ───────────────────────────────────────────────────────────────

def test_template_names_only_the_callbacks_every_deployment_runs():
    """`langfuse_otel` in the template would make the langfuse plugin mandatory for boot."""
    assert _template()["litellm_settings"]["callbacks"] == BASE_CALLBACKS


def test_every_local_deployment_keeps_health_probes_out_of_the_logging_callbacks():
    """LiteLLM's background health checks (every 60 s per probed group) were 57 percent of all
    Langfuse observations. `health_check_params` is merged into the probe request only, and `no-log`
    skips every non-_PROXY_ callback, so tracing sees real traffic alone and spend tracking still runs."""
    for deployment in _template()["model_list"]:
        params = deployment["model_info"].get("health_check_params")
        assert params == {"no-log": True}, f"{deployment['model_name']} probes would be traced: {params!r}"
    # never on litellm_params: there `no-log` would silence tracing for REAL traffic too
    for deployment in _template()["model_list"]:
        assert "no-log" not in deployment["litellm_params"]


def test_template_redacts_user_api_key_info():
    assert _template()["litellm_settings"]["redact_user_api_key_info"] is True


def test_template_sets_the_spend_log_retention_policy():
    """Keys verified against LiteLLM 1.100.1 (proxy_server.py schedules spend_log_cleanup_job from
    exactly these two general_settings)."""
    general = _template()["general_settings"]
    assert general["maximum_spend_logs_retention_period"] == "90d"
    assert general["maximum_spend_logs_cleanup_cron"] == "30 4 * * *"


def test_entrypoint_and_image_run_the_callback_step():
    assert 'python3 /app/add_callbacks.py /tmp/config.yaml "${LITELLM_EXTRA_CALLBACKS:-}"' in (
        ENTRYPOINT.read_text(encoding="utf-8"))
    assert "COPY add_callbacks.py /app/add_callbacks.py" in DOCKERFILE.read_text(encoding="utf-8")


# ── the renderer decides ───────────────────────────────────────────────────────

def test_gateway_gets_the_langfuse_env_only_with_the_plugin():
    on = _gateway_env(["langfuse"])
    for key, value in compose.GATEWAY_LANGFUSE_ENV.items():
        assert on[key] == value
    off = _gateway_env(["monitoring"])
    for key in compose.GATEWAY_LANGFUSE_ENV:
        assert key not in off, f"{key} rendered on model-gateway without the langfuse plugin"


def test_gateway_langfuse_env_values():
    """The exact contract checked against the installed LiteLLM source and a live probe."""
    assert compose.GATEWAY_LANGFUSE_ENV == {
        "LITELLM_EXTRA_CALLBACKS": "langfuse_otel",
        "LANGFUSE_OTEL_HOST": "http://langfuse-web:3000",
        "LANGFUSE_TRACING_ENVIRONMENT": "gateway",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "no_content",
    }
    # V2 sends traces to environment `default` with a NULL-input root span on 1.100.1.
    assert "LITELLM_OTEL_V2" not in compose.GATEWAY_LANGFUSE_ENV


def test_gateway_never_carries_a_langfuse_key_literal():
    """The key pair reaches the gateway only as files; the entrypoint exports them for LiteLLM."""
    env = _gateway_env(["langfuse"])
    assert env["LANGFUSE_PUBLIC_KEY_FILE"] == "/run/secrets/langfuse_public_key"
    assert env["LANGFUSE_SECRET_KEY_FILE"] == "/run/secrets/langfuse_secret_key"
    assert "LANGFUSE_PUBLIC_KEY" not in env and "LANGFUSE_SECRET_KEY" not in env


# ── add_callbacks.py ───────────────────────────────────────────────────────────

def test_parse_callback_names_trims_dedupes_and_drops_empties():
    assert parse_callback_names(" langfuse_otel, ,prometheus,langfuse_otel ") == ["langfuse_otel", "prometheus"]
    assert parse_callback_names("") == []


def test_add_callbacks_appends_after_the_template_callbacks_without_duplicates():
    out = yaml.safe_load(add_callbacks(CONFIG, ["langfuse_otel", "prometheus"]))
    assert out["litellm_settings"]["callbacks"] == BASE_CALLBACKS + ["langfuse_otel"]
    assert out["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"


def test_add_callbacks_creates_the_list_when_absent():
    out = yaml.safe_load(add_callbacks("general_settings: {}\n", ["langfuse_otel"]))
    assert out["litellm_settings"]["callbacks"] == ["langfuse_otel"]


def test_langfuse_otel_without_credentials_is_skipped_not_fatal():
    """Without keys LiteLLM falls back to a console exporter and prints prompts into the log."""
    usable, warnings = usable_callbacks(["langfuse_otel"], {"LANGFUSE_OTEL_HOST": "http://x"})
    assert usable == []
    assert "LANGFUSE_PUBLIC_KEY" in warnings[0] and "LANGFUSE_SECRET_KEY" in warnings[0]
    assert usable_callbacks(["langfuse_otel"], LANGFUSE_ENV) == (["langfuse_otel"], [])


def test_cli_empty_value_is_a_no_op(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    assert main([str(cfg), ""], {}) == 0
    assert cfg.read_text() == CONFIG


def test_cli_enables_langfuse_otel_with_credentials(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    assert main([str(cfg), "langfuse_otel"], LANGFUSE_ENV) == 0
    assert yaml.safe_load(cfg.read_text())["litellm_settings"]["callbacks"][-1] == "langfuse_otel"


def test_cli_skips_langfuse_otel_without_credentials_and_leaves_the_config(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    assert main([str(cfg), "langfuse_otel"], {}) == 0
    assert cfg.read_text() == CONFIG
    assert "skipping callback 'langfuse_otel'" in capsys.readouterr().err


def test_cli_malformed_config_exits_2(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("litellm_settings:\n  callbacks: prometheus\n")
    assert main([str(cfg), "langfuse_otel"], LANGFUSE_ENV) == 2
