"""Unit tests for openclaw/scripts/merge_gateway_config.py (channel SecretRef injection)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "openclaw" / "scripts" / "merge_gateway_config.py"
_spec = importlib.util.spec_from_file_location("merge_gateway_config", _SCRIPT)
assert _spec and _spec.loader
mg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mg)


def test_secret_ref_shape() -> None:
    assert mg._secret_ref("FOO") == {"source": "env", "provider": "default", "id": "FOO"}


def test_normalize_env_secret_id_hyphens() -> None:
    assert mg._normalize_env_secret_id("DISCORD-BOT-TOKEN") == "DISCORD_BOT_TOKEN"
    assert mg._normalize_env_secret_id("discord_bot_token") == "DISCORD_BOT_TOKEN"


def test_sanitize_channel_refs_hyphenated_id() -> None:
    data: dict = {
        "channels": {
            "discord": {
                "token": {
                    "source": "env",
                    "provider": "default",
                    "id": "DISCORD-BOT-TOKEN",
                },
            },
        },
    }
    assert mg._sanitize_channel_env_secret_refs(data) is True
    assert data["channels"]["discord"]["token"]["id"] == "DISCORD_BOT_TOKEN"


def test_sanitize_channel_refs_provider_case() -> None:
    data: dict = {
        "channels": {
            "discord": {
                "token": {"source": "env", "provider": "Default", "id": "DISCORD_BOT_TOKEN"},
            },
        },
    }
    assert mg._sanitize_channel_env_secret_refs(data) is True
    assert data["channels"]["discord"]["token"]["provider"] == "default"


def test_inject_discord_from_discord_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    data: dict = {"channels": {"discord": {"enabled": True, "token": "plaintext"}}}
    assert mg._inject_channel_secret_refs(data) is True
    assert data["channels"]["discord"]["token"] == {
        "source": "env",
        "provider": "default",
        "id": "DISCORD_TOKEN",
    }


def test_inject_discord_from_discord_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "y")
    data: dict = {"channels": {"discord": {}}}
    assert mg._inject_channel_secret_refs(data) is True
    assert data["channels"]["discord"]["token"] == {
        "source": "env",
        "provider": "default",
        "id": "DISCORD_BOT_TOKEN",
    }


def test_inject_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg")
    data: dict = {"channels": {"telegram": {"enabled": True, "botToken": "old"}}}
    assert mg._inject_channel_secret_refs(data) is True
    assert data["channels"]["telegram"]["botToken"] == {
        "source": "env",
        "provider": "default",
        "id": "TELEGRAM_BOT_TOKEN",
    }


def test_no_env_leaves_tokens_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    data: dict = {"channels": {"discord": {"token": "keep-me"}, "telegram": {"botToken": "keep-tg"}}}
    assert mg._inject_channel_secret_refs(data) is False


def test_channels_not_dict_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    data: dict = {"channels": "broken"}
    assert mg._inject_channel_secret_refs(data) is False


def test_merge_elevated_allow_webchat_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCLAW_ELEVATED_ALLOW_WEBCHAT", raising=False)
    data: dict = {"tools": {}}
    assert mg._merge_elevated_allow_webchat(data) is False
    assert "elevated" not in data["tools"] or data["tools"].get("elevated") == {}


def test_merge_elevated_allow_webchat_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLAW_ELEVATED_ALLOW_WEBCHAT", "1")
    data: dict = {"tools": {}}
    assert mg._merge_elevated_allow_webchat(data) is True
    assert data["tools"]["elevated"]["enabled"] is True
    assert data["tools"]["elevated"]["allowFrom"]["webchat"] == ["*"]


def test_sanitize_elevated_allow_from_booleans() -> None:
    data: dict = {"tools": {"elevated": {"allowFrom": {"webchat": True, "discord": True}}}}
    assert mg._sanitize_elevated_allow_from_legacy_booleans(data) is True
    assert data["tools"]["elevated"]["allowFrom"]["webchat"] == ["*"]
    assert data["tools"]["elevated"]["allowFrom"]["discord"] == ["*"]


def test_merge_discord_guild_allowlist_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCLAW_DISCORD_GUILD_IDS", raising=False)
    data: dict = {"channels": {"discord": {"enabled": True}}}
    assert mg._merge_discord_guild_allowlist_from_env(data) is False


def test_merge_discord_guild_allowlist_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLAW_DISCORD_GUILD_IDS", "111111111111111111, 222222222222222222")
    data: dict = {"channels": {"discord": {"enabled": True, "groupPolicy": "allowlist"}}}
    assert mg._merge_discord_guild_allowlist_from_env(data) is True
    assert data["channels"]["discord"]["guilds"]["111111111111111111"]["requireMention"] is False
    assert data["channels"]["discord"]["guilds"]["222222222222222222"]["requireMention"] is False


def test_merge_discord_guild_allowlist_removes_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guilds removed from OPENCLAW_DISCORD_GUILD_IDS are removed from openclaw.json."""
    monkeypatch.setenv("OPENCLAW_DISCORD_GUILD_IDS", "111111111111111111")
    data: dict = {"channels": {"discord": {"guilds": {
        "111111111111111111": {"requireMention": False},
        "999999999999999999": {"requireMention": False},  # stale — not in env
    }}}}
    assert mg._merge_discord_guild_allowlist_from_env(data) is True
    guilds = data["channels"]["discord"]["guilds"]
    assert "111111111111111111" in guilds
    assert "999999999999999999" not in guilds


def test_merge_discord_user_allowlist_scoped_to_env_guilds(monkeypatch: pytest.MonkeyPatch) -> None:
    """User IDs are applied only to env-managed guilds, not to auto-discovered ones."""
    monkeypatch.setenv("OPENCLAW_DISCORD_GUILD_IDS", "111111111111111111")
    monkeypatch.setenv("OPENCLAW_DISCORD_USER_IDS", "555555555555555555")
    data: dict = {"channels": {"discord": {"guilds": {
        "111111111111111111": {"requireMention": False},   # env-managed
        "888888888888888888": {"requireMention": False},   # auto-discovered — must not be touched
    }}}}
    assert mg._merge_discord_user_allowlist_from_env(data) is True
    guilds = data["channels"]["discord"]["guilds"]
    assert guilds["111111111111111111"].get("users") == ["555555555555555555"]
    assert "users" not in guilds["888888888888888888"]


def test_merge_discord_user_allowlist_replaces_not_appends(monkeypatch: pytest.MonkeyPatch) -> None:
    """User list is set to exactly the env value, not accumulated across runs."""
    monkeypatch.setenv("OPENCLAW_DISCORD_GUILD_IDS", "111111111111111111")
    monkeypatch.setenv("OPENCLAW_DISCORD_USER_IDS", "555555555555555555")
    data: dict = {"channels": {"discord": {"guilds": {
        "111111111111111111": {"requireMention": False, "users": ["555555555555555555", "old_user"]},
    }}}}
    mg._merge_discord_user_allowlist_from_env(data)
    assert data["channels"]["discord"]["guilds"]["111111111111111111"]["users"] == ["555555555555555555"]


def test_merge_unrestricted_gateway_container_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCLAW_UNRESTRICTED_GATEWAY_CONTAINER", raising=False)
    data: dict = {"tools": {}}
    assert mg._merge_unrestricted_gateway_container(data) is False


def test_merge_deny_builtin_browser_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCLAW_ALLOW_BUILTIN_BROWSER", raising=False)
    data: dict = {"tools": {"web": {"search": {"enabled": False}}}}
    assert mg._merge_deny_builtin_browser_unless_opt_in(data) is True
    assert data["tools"]["deny"] == ["browser"]


def test_merge_deny_builtin_browser_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCLAW_ALLOW_BUILTIN_BROWSER", raising=False)
    data: dict = {"tools": {"deny": ["browser"]}}
    assert mg._merge_deny_builtin_browser_unless_opt_in(data) is False


def test_merge_deny_builtin_browser_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLAW_ALLOW_BUILTIN_BROWSER", "1")
    data: dict = {"tools": {}}
    assert mg._merge_deny_builtin_browser_unless_opt_in(data) is False
    assert "deny" not in data["tools"]


def test_merge_unrestricted_gateway_container_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLAW_UNRESTRICTED_GATEWAY_CONTAINER", "1")
    monkeypatch.delenv("OPENCLAW_ELEVATED_ALLOW_WEBCHAT", raising=False)
    data: dict = {"tools": {"web": {"search": {"enabled": False}}}, "agents": {"defaults": {}}}
    assert mg._merge_unrestricted_gateway_container(data) is True
    assert data["tools"]["exec"]["host"] == "gateway"
    assert data["tools"]["exec"]["security"] == "full"
    assert data["tools"]["exec"]["ask"] == "off"
    assert data["tools"]["elevated"]["enabled"] is True
    assert data["tools"]["elevated"]["allowFrom"]["webchat"] == ["*"]
    assert data["tools"]["elevated"]["allowFrom"]["discord"] == ["*"]
    assert data["agents"]["defaults"]["elevatedDefault"] == "full"
