"""Integration-free smoke tests for the voice (STT/TTS) service definitions.

Checks:
- docker-compose.yml declares stt + tts under profile "voice" with the correct
  images and no published host ports.
- ops-controller ALLOWED_SERVICES and GPU_ASSIGNABLE_SERVICES include "stt" and "tts".
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# docker-compose.yml assertions
# ---------------------------------------------------------------------------

def _load_compose() -> dict:
    compose_path = _REPO_ROOT / "docker-compose.yml"
    return yaml.safe_load(compose_path.read_text(encoding="utf-8"))


def test_stt_service_exists():
    compose = _load_compose()
    assert "stt" in compose["services"], "stt service not found in docker-compose.yml"


def test_tts_service_exists():
    compose = _load_compose()
    assert "tts" in compose["services"], "tts service not found in docker-compose.yml"


def test_stt_has_voice_profile():
    compose = _load_compose()
    stt = compose["services"]["stt"]
    assert "voice" in stt.get("profiles", []), \
        f"stt profiles={stt.get('profiles')} — expected 'voice'"


def test_tts_has_voice_profile():
    compose = _load_compose()
    tts = compose["services"]["tts"]
    assert "voice" in tts.get("profiles", []), \
        f"tts profiles={tts.get('profiles')} — expected 'voice'"


def test_stt_image_contains_faster_whisper():
    compose = _load_compose()
    image = compose["services"]["stt"].get("image", "")
    assert "faster-whisper-server" in image, \
        f"stt image '{image}' does not contain 'faster-whisper-server'"


def test_tts_image_contains_kokoro():
    compose = _load_compose()
    image = compose["services"]["tts"].get("image", "")
    assert "kokoro" in image, f"tts image '{image}' does not contain 'kokoro'"


def test_stt_has_no_host_ports():
    compose = _load_compose()
    stt = compose["services"]["stt"]
    assert "ports" not in stt or not stt["ports"], \
        f"stt must not publish host ports (found: {stt.get('ports')})"


def test_tts_has_no_host_ports():
    compose = _load_compose()
    tts = compose["services"]["tts"]
    assert "ports" not in tts or not tts["ports"], \
        f"tts must not publish host ports (found: {tts.get('ports')})"


def test_stt_on_backend_network():
    compose = _load_compose()
    stt = compose["services"]["stt"]
    networks = stt.get("networks", [])
    assert "backend" in networks, \
        f"stt must be on the backend network (found: {networks})"


def test_tts_on_backend_network():
    compose = _load_compose()
    tts = compose["services"]["tts"]
    networks = tts.get("networks", [])
    assert "backend" in networks, \
        f"tts must be on the backend network (found: {networks})"


# ---------------------------------------------------------------------------
# ops-controller allowlist assertions
# ---------------------------------------------------------------------------

# Mock docker before importing ops-controller so the import doesn't fail in CI
sys.modules.setdefault("docker", MagicMock())

_oc_path = _REPO_ROOT / "ops-controller" / "main.py"
_oc_spec = importlib.util.spec_from_file_location("oc_voice_test", _oc_path)
_oc = importlib.util.module_from_spec(_oc_spec)
_oc_spec.loader.exec_module(_oc)


def test_stt_in_allowed_services():
    assert "stt" in _oc.ALLOWED_SERVICES, \
        f"stt not in ALLOWED_SERVICES: {_oc.ALLOWED_SERVICES}"


def test_tts_in_allowed_services():
    assert "tts" in _oc.ALLOWED_SERVICES, \
        f"tts not in ALLOWED_SERVICES: {_oc.ALLOWED_SERVICES}"


def test_stt_in_gpu_assignable_services():
    assert "stt" in _oc.GPU_ASSIGNABLE_SERVICES, \
        f"stt not in GPU_ASSIGNABLE_SERVICES: {_oc.GPU_ASSIGNABLE_SERVICES}"


def test_tts_in_gpu_assignable_services():
    assert "tts" in _oc.GPU_ASSIGNABLE_SERVICES, \
        f"tts not in GPU_ASSIGNABLE_SERVICES: {_oc.GPU_ASSIGNABLE_SERVICES}"
