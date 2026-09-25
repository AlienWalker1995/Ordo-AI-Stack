"""The operator source (ordo.yaml) is a closed schema: an unknown key is a typo or a retired option,
and silently dropping it changes what renders (`plguins:` falls back to `plugins: auto`)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ordo.host import wizard
from ordo.render.config import Source

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_unknown_top_level_key_is_rejected_with_a_suggestion():
    with pytest.raises(ValueError, match=r"'plguins'.*did you mean 'plugins'"):
        Source.from_dict({"model": "auto", "plguins": ["comfyui"]})


def test_unknown_top_level_key_without_a_close_match_names_the_valid_keys():
    with pytest.raises(ValueError, match=r"'zzz'.*valid keys"):
        Source.from_dict({"zzz": 1})


def test_retired_cloud_fallback_key_is_rejected_with_a_migration_hint():
    with pytest.raises(ValueError, match=r"'cloud_fallback'.*removed.*delete"):
        Source.from_dict({"cloud_fallback": {"enabled": False}})


def test_unknown_hardware_key_is_rejected_with_a_suggestion():
    with pytest.raises(ValueError, match=r"hardware.*'ram_gbs'.*did you mean 'ram_gb'"):
        Source.from_dict({"hardware": {"gpus": [], "ram_gbs": 64}})


def test_unknown_hardware_gpu_key_is_rejected_with_a_suggestion():
    with pytest.raises(ValueError, match=r"hardware\.gpus\[0\].*'vram'.*did you mean 'vram_gb'"):
        Source.from_dict({"hardware": {"gpus": [{"name": "RTX 5090", "vram": 32}]}})


def test_explicit_hardware_spec_loads():
    spec = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": "GPU-x"}],
            "ram_gb": 128, "cpu_cores": 32, "platform": "Linux"}
    assert Source.from_dict({"hardware": spec}).hardware == spec


@pytest.mark.parametrize("key", ["data_path", "1PATH", "DATA-PATH", "DATA PATH", "_X"])
def test_site_keys_must_be_env_var_shaped(key):
    with pytest.raises(ValueError, match=rf"site.*{key!r}"):
        Source.from_dict({"site": {key: "/srv"}})


def test_site_stays_free_form_for_env_shaped_keys():
    site = {"DATA_PATH": "/srv/ordo/data", "N8N_WEBHOOK_URL": "https://x", "HERMES_MAX_TOKENS": 4096}
    assert Source.from_dict({"site": site}).site == site


def test_tracked_example_source_loads():
    Source.load(REPO_ROOT / "ordo.example.yaml")


def test_wizard_written_source_loads(tmp_path):
    src = wizard.build_source({
        "hardware": {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 64, "cpu_cores": 16,
                     "platform": "Linux"},
        "tier": "high", "model": "auto", "plugins": ["comfyui"],
        "overrides": {"llamacpp": {"ctx_size": 131072}},
        "site": {"DATA_PATH": "/srv/ordo/data", "BASE_PATH": "/srv/ordo",
                 "CADDY_TAILNET_HOSTNAME": "ordo.tail1234.ts.net", "CADDY_BIND": "0.0.0.0"},
    })
    path = wizard.write_source(src, tmp_path / "ordo.yaml")
    loaded = Source.load(path)
    assert loaded.plugins == ["comfyui"]
    assert loaded.site["CADDY_TAILNET_HOSTNAME"] == "ordo.tail1234.ts.net"
