"""Scheduler status contract + doctor support bundle."""
from pathlib import Path

from ordo import doctor
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.scheduler import Job, Scheduler

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")


# --- status contract (the dashboard/agents poll this for "GPU busy, ~Ns") ---
def test_status_idle():
    st = Scheduler(32).status()
    assert st["state"] == "idle"
    assert st["free_vram_gb"] == 32
    assert st["running"] == [] and st["queued"] == []
    assert st["eta_seconds"] is None


def test_status_busy_with_eta_while_waiting():
    s = Scheduler(32)
    s.submit(Job("m1", 20, "media", est_seconds=120))
    s.submit(Job("m2", 20, "media", est_seconds=60))
    s.pump()                       # m1 runs, m2 can't fit -> queued
    s.tick(30)
    st = s.status()
    assert st["state"] == "busy"
    assert st["waiting_on_vram"] is True
    assert st["eta_seconds"] == 90.0            # m1: 120 - 30 elapsed
    assert st["running"][0]["remaining_s"] == 90.0
    assert st["queued"][0]["id"] == "m2"


def test_status_no_eta_when_nothing_waiting():
    s = Scheduler(32)
    s.submit(Job("chat", 4, "chat", est_seconds=10))
    s.pump()
    st = s.status()
    assert st["state"] == "busy" and st["waiting_on_vram"] is False
    assert st["eta_seconds"] is None            # nothing queued -> no ETA needed


# --- doctor support bundle ---
def test_bundle_redacts_secrets():
    env = {"OPENAI_API_KEY": "sk-secret", "HF_TOKEN": "hf_x", "LLAMACPP_CTX_SIZE": "131072"}
    out = doctor._sanitize_env(env)
    assert out["OPENAI_API_KEY"] == "<redacted>"
    assert out["HF_TOKEN"] == "<redacted>"
    assert out["LLAMACPP_CTX_SIZE"] == "131072"   # non-secret preserved


def test_bundle_structure():
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": "auto"})
    b = doctor.collect_bundle(src, CATALOG, REGISTRY)
    assert b["sizing"]["model"] == "huihui-qwen3.6-27b-q6"
    assert "song-gen" in b["plugins_enabled"]
    assert "unpinned_sha256" in b["catalog"]
    # rendered env is present and carries no raw secret values
    assert "LLAMACPP_CTX_SIZE" in b["rendered_env"]
