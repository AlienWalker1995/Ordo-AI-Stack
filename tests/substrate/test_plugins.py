"""Registry-driven plugin resolution: hardware gating, dependencies, env composition."""
from pathlib import Path

from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")

P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32}
P_CPU = {"gpus": [], "ram_gb": 16, "cpu_cores": 8}
P_8GB = {"gpus": [{"name": "RTX 3070", "vram_gb": 8}], "ram_gb": 32, "cpu_cores": 12}
# The real host: primary 5090 (compute) + secondary Pascal 1070 (voice must land here).
UUID_5090 = "GPU-97fe65ee-5e2d-8c9b-32d0-362f510ceb96"
UUID_1070 = "GPU-20fac13a-5e5b-1818-581f-63901612fd84"
P_DUAL = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": UUID_5090},
                   {"name": "GTX 1070", "vram_gb": 8, "uuid": UUID_1070}],
          "ram_gb": 128, "cpu_cores": 32}


# Every site key a plugin requires, so the hardware-gating tests below see only the hardware gate.
FULL_SITE = {"CADDY_BIND": "127.0.0.1", "CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
             "CADDY_TAILNET_DOMAIN": "example.ts.net", "SSO_ALLOWED_EMAILS": "me@example.com", "MEMORY_VAULT_PATH": "/srv/vault"}


def _src(**kw):
    base = {"hardware": "auto", "tier": "auto", "model": "auto", "plugins": "auto", "site": FULL_SITE}
    base.update(kw)
    return Source.from_dict(base)


# The CPU-ok service plugins — enable on ANY hardware (they run without a GPU), but stay dormant
# behind their compose profile until requested. Voice/comfyui/song-gen are the GPU-gated ones
# handled separately. The tailnet-name sidecars are opt-in (they need a tagged Tailscale key), so
# `plugins: auto` leaves them out.
CPU_OK_SERVICE_PLUGINS = {"monitoring", "rag", "automation", "open-webui",
                          "searxng-web", "codebase-memory-ui", "hermes-dashboard", "edge",
                          "obsidian-livesync"}


def test_registry_loaded_manifests():
    ids = {p.id for p in REGISTRY.plugins}
    assert {"comfyui", "song-gen", "voice", "monitoring"} <= ids
    # the ported V1-parity plugins are registered too
    assert {"rag", "automation", "open-webui", "searxng-web",
            "codebase-memory-ui", "hermes-dashboard", "edge"} <= ids


def test_big_gpu_enables_all_and_merges_env():
    # single 5090: media enables; the CPU-ok service plugins enable; voice needs a SECOND card → off.
    # llamacpp-cpu (CPU LLM fallback) is CPU-ok but RAM-gated at 24GB, so it rides big-RAM hosts like
    # this 128GB 5090 box (but NOT the 16GB P_CPU profile — see test_cpu_disables_voice_and_media).
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert set(rc.plugins_enabled) == {"comfyui", "song-gen", "ltx-trainer", "llamacpp-cpu"} | CPU_OK_SERVICE_PLUGINS
    assert "voice" not in rc.plugins_enabled
    assert rc.env["COMFYUI_ENABLED"] == "1"
    assert "ltx-trainer" in rc.plugins_enabled    # LoRA trainer enables on a big single GPU too
    assert rc.env["SONG_GEN_ENABLED"] == "1"
    assert rc.env["RAG_ENABLED"] == "1"           # a ported plugin's env fragment merges too
    assert "media" in rc.compose_profiles and "rag" in rc.compose_profiles


def test_dual_gpu_enables_voice_pinned_to_secondary():
    # 5090 + 1070: everything including voice; voice pins to the 1070's uuid (Pascal kernels)
    rc = render(_src(hardware=P_DUAL), CATALOG, REGISTRY)
    assert {"comfyui", "song-gen", "voice", "monitoring"} <= set(rc.plugins_enabled)
    assert "voice" in rc.compose_profiles
    c = rc.compose_dict()
    for svc in ("stt", "tts"):
        env = c["services"][svc]["environment"]
        assert env["CUDA_VISIBLE_DEVICES"] == UUID_1070
        assert env["NVIDIA_VISIBLE_DEVICES"] == UUID_1070
        dev = c["services"][svc]["deploy"]["resources"]["reservations"]["devices"][0]
        assert dev["device_ids"] == [UUID_1070]           # pinned card, not `count: all`


def test_single_gpu_disables_voice_with_warning():
    # only the 5090: voice images crash there → gated OFF, never fall back to the primary
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert "voice" not in rc.plugins_enabled
    assert any("SECONDARY GPU" in w for w in rc.warnings)


def test_cpu_disables_voice_and_media():
    rc = render(_src(hardware=P_CPU), CATALOG, REGISTRY)
    assert "voice" not in rc.plugins_enabled              # CPU-only → voice off
    assert not ({"comfyui", "song-gen"} & set(rc.plugins_enabled))
    assert "COMFYUI_ENABLED" not in rc.env
    assert set(rc.plugins_enabled) == CPU_OK_SERVICE_PLUGINS


def test_small_gpu_gates_by_vram():
    # single 8GB card: comfyui(6) fits; song-gen(20) does not; voice needs a 2nd card → off
    rc = render(_src(hardware=P_8GB), CATALOG, REGISTRY)
    assert "comfyui" in rc.plugins_enabled
    assert "voice" not in rc.plugins_enabled
    assert "song-gen" not in rc.plugins_enabled


def test_dependency_drops_plugin_when_dep_absent():
    # explicitly ask for song-gen only (no comfyui) → dep unmet → dropped with a note
    rc = render(_src(hardware=P_5090, plugins=["song-gen"]), CATALOG, REGISTRY)
    assert "song-gen" not in rc.plugins_enabled
    assert any("dependency" in w for w in rc.warnings)


def test_explicit_list_respected():
    rc = render(_src(hardware=P_5090, plugins=["comfyui"]), CATALOG, REGISTRY)
    assert rc.plugins_enabled == ["comfyui"]


def test_comfyui_alloc_conf_never_empty(tmp_path):
    # Live-only crash class: an EMPTY PYTORCH_CUDA_ALLOC_CONF (present-but-blank) makes torch's
    # allocator parser raise `ValueError: Unrecognized key ',' ...` at torch._C._cuda_init(),
    # crash-looping ComfyUI on boot. V1 sets a real value; V2 must not render "".
    import yaml
    rc = render(_src(hardware=P_5090, plugins=["comfyui"]), CATALOG, REGISTRY)
    rc.write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    env = c["services"]["comfyui"]["environment"]
    val = env.get("PYTORCH_CUDA_ALLOC_CONF", "")
    # after ${VAR:-default} substitution the rendered default must be non-empty and contain a real key
    assert val and val.strip() not in ("", ",") and "expandable_segments" in val, \
        f"PYTORCH_CUDA_ALLOC_CONF must render a valid non-empty allocator config, got {val!r}"


def test_ltx_trainer_manifest_invariants():
    # LTX-trainer is the stack's sole LoRA trainer (ai-toolkit retired 2026-07-24).
    from pathlib import Path

    import yaml
    manifest = Path(__file__).resolve().parents[2] / "services" / "ltx-trainer" / "plugin.yaml"
    m = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    svc = m["services"][0]
    assert svc["gpu_pin"] == "primary"                     # trainer needs the 5090 (cu128 torch)
    assert "ports" not in svc                              # headless CLI, no host publish / UI
    assert svc["image"].startswith("${LTX_TRAINER_IMAGE:-ordo/ltx-trainer:")  # locally built, SHA-pinned
    # the lease seam: the wrapper must be mounted at /ordo/lease-exec.py, read-only
    assert any(v.endswith(":/ordo/lease-exec.py:ro") for v in svc["volumes"])
    # HF cache must be a NAMED volume — an NTFS/9p bind mmap-hangs sharded loads.
    hub = next(v for v in svc["volumes"] if "/root/.cache/huggingface/hub" in v)
    assert not hub.startswith(("$", ".", "/")), "hub cache must be a named volume, not a bind"
    # datasets are reparented onto this plugin's own data dir — no residual ai-toolkit path.
    assert not any("/ai-toolkit/" in v for v in svc["volumes"]), "no dead ai-toolkit paths"
    assert svc["env"]["ORDO_LEASE_KIND"] == "training"
    assert svc.get("shm_size"), "trainer needs a real shm_size — torch pins tensors via /dev/shm"
    # Secret-backed keys must come from secrets.env (env_file) ONLY. An `environment:` entry like
    # `HF_TOKEN: ${HF_TOKEN:-}` substitutes EMPTY from the rendered .env and OVERRIDES the real
    # env_file value: huggingface_hub then sends a blank Bearer header and crashes the trainer
    # (found live 2026-07-15).
    for key in list(m["secrets"]) + ["OPS_CONTROLLER_TOKEN"]:
        assert key not in svc["env"], f"{key} must not be re-declared in the env block"


# ── requires.site: the site keys a plugin cannot run without ─────────────────────────────────────
EDGE_SITE_KEYS = ("CADDY_BIND", "CADDY_TAILNET_HOSTNAME", "CADDY_TAILNET_DOMAIN", "SSO_ALLOWED_EMAILS")
# Keys a manifest may reference as ${KEY:?} without declaring them in requires.site: the host
# roots every render carries (ordo init records them), and COMFYUI_URL, which the render derives.
_NOT_SITE_KEYS = {"BASE_PATH", "DATA_PATH", "COMFYUI_URL"}


def test_manifests_declare_required_site_keys():
    assert REGISTRY.get("edge").site_keys == EDGE_SITE_KEYS
    assert REGISTRY.get("memory-vault").site_keys == ("MEMORY_VAULT_PATH",)
    assert REGISTRY.get("evals").site_keys == ("MEMORY_VAULT_PATH",)


def test_every_fail_loud_site_ref_is_declared():
    # A ${KEY:?} ref is a key compose refuses to run without. Each one in a manifest must be in
    # that plugin's requires.site, so the render gates it instead of a late compose failure.
    import json
    import re

    import yaml
    for manifest in sorted((ROOT / "services").glob("*/plugin.yaml")):
        values = json.dumps(yaml.safe_load(manifest.read_text(encoding="utf-8")))  # comments dropped
        refs = set(re.findall(r"\$\{([A-Z0-9_]+):\?", values))
        plugin_id = manifest.parent.name
        undeclared = refs - _NOT_SITE_KEYS - set(REGISTRY.get(plugin_id).site_keys)
        assert not undeclared, f"{plugin_id}: ${{KEY:?}} refs missing from requires.site: {undeclared}"


def test_auto_skips_plugin_missing_site_keys_and_its_dependents():
    rc = render(Source.from_dict({"hardware": P_CPU, "plugins": "auto"}), CATALOG, REGISTRY)
    enabled = set(rc.plugins_enabled) | {s["plugin_id"] for s in rc.mcp_servers}
    assert "edge" not in enabled
    assert "memory-vault" not in enabled
    # dependents of a skipped plugin are dropped by the dependency closure
    assert "hermes-dashboard" not in enabled and "tailnet-names" not in enabled
    edge_note = next(w for w in rc.warnings if "'edge'" in w and "site" in w)
    assert all(key in edge_note for key in EDGE_SITE_KEYS)
    assert "ordo render" in edge_note
    assert any("'memory-vault'" in w and "MEMORY_VAULT_PATH" in w for w in rc.warnings)


def test_auto_enables_plugin_once_site_keys_are_set():
    rc = render(Source.from_dict({"hardware": P_CPU, "plugins": "auto", "site": FULL_SITE}), CATALOG, REGISTRY)
    assert {"edge", "hermes-dashboard"} <= set(rc.plugins_enabled)
    assert "tailnet-names" not in rc.plugins_enabled    # opt-in: listed by id, never by auto
    assert "memory-vault" in {s["plugin_id"] for s in rc.mcp_servers}
    assert not any("site key" in w for w in rc.warnings)


def test_auto_treats_blank_site_key_as_missing():
    site = {"CADDY_BIND": " ", "CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
            "CADDY_TAILNET_DOMAIN": "example.ts.net", "SSO_ALLOWED_EMAILS": "me@example.com"}
    rc = render(Source.from_dict({"hardware": P_CPU, "plugins": "auto", "site": site}), CATALOG, REGISTRY)
    assert "edge" not in rc.plugins_enabled
    assert any("'edge'" in w and "CADDY_BIND" in w for w in rc.warnings)


def test_explicit_plugin_missing_site_keys_is_a_render_error():
    import pytest
    src = Source.from_dict({"hardware": P_CPU, "plugins": ["edge", "memory-vault"]})
    with pytest.raises(ValueError) as err:
        render(src, CATALOG, REGISTRY)
    message = str(err.value)
    assert "'edge'" in message and all(key in message for key in EDGE_SITE_KEYS)
    assert "'memory-vault'" in message and "MEMORY_VAULT_PATH" in message


def test_searxng_runs_the_tracked_settings_read_only():
    """SearXNG's engine selection is part of the stack, not of one host's data dir: the upstream
    defaults were all captcha-blocked, so a fresh install got a search tool that found nothing.
    The settings are tracked in services/searxng-web/ and mounted read-only; the secret_key comes
    from SEARXNG_SECRET (secrets.env), never from the tracked file."""
    import yaml

    rc = render(Source.from_dict({"hardware": {"gpus": [], "ram_gb": 32}, "model": "auto",
                                  "plugins": ["searxng-web", "searxng"]}), CATALOG, REGISTRY)
    searxng = rc.compose_dict()["services"]["searxng"]
    mounts = [v for v in searxng["volumes"] if ":/etc/ordo-searxng" in v]
    assert mounts == ["${BASE_PATH:?BASE_PATH must be set (non-empty)}/services/searxng-web/settings:/etc/ordo-searxng:ro"]
    assert not [v for v in searxng["volumes"] if "DATA_PATH" in v]
    assert searxng["environment"]["SEARXNG_SETTINGS_PATH"] == "/etc/ordo-searxng/settings.yml"
    assert searxng["environment"]["SEARXNG_SECRET"].startswith("${SEARXNG_SECRET")

    settings = yaml.safe_load((ROOT / "services" / "searxng-web" / "settings" / "settings.yml").read_text(encoding="utf-8"))
    assert "secret_key" not in (settings.get("server") or {})
    enabled = {e["name"] for e in settings["engines"] if e.get("disabled") is False}
    assert {"bing", "yandex"} <= enabled
