from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MERGE_SCRIPT = REPO_ROOT / "openclaw" / "scripts" / "merge_gateway_config.py"
OPENCLAW_CONFIG = REPO_ROOT / "data" / "openclaw" / "openclaw.json"


def test_merge_script_pins_canonical_local_chat_primary():
    text = MERGE_SCRIPT.read_text(encoding="utf-8")

    # Always emit local-chat regardless of LLAMACPP_MODEL value.
    assert 'OPENCLAW_PRIMARY_MODEL_ID = "local-chat"' in text
    assert 'desired_primary = f"gateway/{OPENCLAW_PRIMARY_MODEL_ID}"' in text


def test_openclaw_config_uses_canonical_local_chat_primary():
    text = OPENCLAW_CONFIG.read_text(encoding="utf-8")

    assert '"id": "local-chat"' in text
    assert '"primary": "gateway/local-chat"' in text
    # GGUF basenames should not appear as model identities anymore.
    assert "google_gemma-4-31B-it-Q4_K_M.gguf" not in text
