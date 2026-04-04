#!/usr/bin/env python3
"""Merge gateway provider into openclaw.json. Fetches models from model-gateway (llama.cpp via gateway).
Injects OPENCLAW_GATEWAY_TOKEN from env when set.
When DISCORD_TOKEN / DISCORD_BOT_TOKEN or TELEGRAM_BOT_TOKEN is set in the environment,
rewrites channel secrets to OpenClaw SecretRef form so tokens are not stored plaintext in JSON."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path


def _repo_root() -> Path:
    """openclaw/scripts -> repo root."""
    return Path(__file__).resolve().parent.parent.parent


def _load_env_file(path: Path) -> None:
    """Set os.environ from a simple KEY=VAL .env file (only keys not already set)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ[key] = val


_load_env_file(_repo_root() / ".env")

GATEWAY_PROVIDER = {
    "baseUrl": "http://model-gateway:11435/v1",
    "apiKey": "local",
    "api": "openai-responses",
    "headers": {"X-Service-Name": "openclaw"},
}

MODEL_GATEWAY_URL = os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:11435")
# Default OpenClaw model metadata to the server cap unless a lower compaction target is set explicitly.
_ctx_raw = os.environ.get("OPENCLAW_CONTEXT_WINDOW", os.environ.get("LLAMACPP_CTX_SIZE", "262144")).strip()
LLAMACPP_CTX = int(_ctx_raw) if _ctx_raw.isdigit() and int(_ctx_raw) > 0 else 262144

# OpenClaw 2026.3.x: tools.elevated.allowFrom.<provider> is a string[] (sender allowlist), not boolean.
_ELEVATED_ALLOW_ALL_SENDERS = ["*"]


def _sanitize_elevated_allow_from_legacy_booleans(data: dict) -> bool:
    """Convert invalid allowFrom boolean true (from older merge) to ['*'] so the gateway starts."""
    modified = False
    tools = data.get("tools")
    if not isinstance(tools, dict):
        return False
    elev = tools.get("elevated")
    if not isinstance(elev, dict):
        return False
    af = elev.get("allowFrom")
    if not isinstance(af, dict):
        return False
    for k, v in list(af.items()):
        if v is True:
            af[k] = list(_ELEVATED_ALLOW_ALL_SENDERS)
            modified = True
    return modified


def _set_elevated_allow_from_all(allow_from: dict, key: str) -> bool:
    """Set allowFrom[key] to ['*'] if missing, empty list, or legacy boolean (handled by sanitizer)."""
    modified = False
    cur = allow_from.get(key)
    if cur is True or cur is None:
        allow_from[key] = list(_ELEVATED_ALLOW_ALL_SENDERS)
        modified = True
    elif isinstance(cur, list) and len(cur) == 0:
        allow_from[key] = list(_ELEVATED_ALLOW_ALL_SENDERS)
        modified = True
    return modified


# Default models when model-gateway is unreachable. First entry is the default.
# Use bare GGUF filenames — the gateway resolves provider from the ID.
DEFAULT_GATEWAY_MODELS = [
    {"id": "Qwen3.5-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf", "name": "Qwen3.5 35B A3B Uncensored Q4",
     "reasoning": True, "input": ["text"],
     "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, "contextWindow": LLAMACPP_CTX, "maxTokens": 16384},
    {"id": "deepseek-r1:7b", "name": "DeepSeek R1 7B", "reasoning": True, "input": ["text"],
     "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, "contextWindow": LLAMACPP_CTX, "maxTokens": 8192},
    {"id": "qwen3:14b", "name": "Qwen3 14B", "reasoning": True, "input": ["text"],
     "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, "contextWindow": LLAMACPP_CTX, "maxTokens": 8192},
    {"id": "deepseek-coder:6.7b", "name": "DeepSeek Coder 6.7B", "reasoning": False, "input": ["text"],
     "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, "contextWindow": LLAMACPP_CTX, "maxTokens": 8192},
    {"id": "llama3.2-vision:11b", "name": "Llama 3.2 Vision 11B", "reasoning": False, "input": ["text", "image"],
     "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, "contextWindow": LLAMACPP_CTX, "maxTokens": 8192},
    {"id": "nomic-embed-text:latest", "name": "Nomic Embed", "reasoning": False, "input": ["text"],
     "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, "contextWindow": min(LLAMACPP_CTX, 8192), "maxTokens": 8192},
]


def _normalize_env_secret_id(raw: str) -> str:
    """Match OpenClaw EnvSecretRef id: /^[A-Z][A-Z0-9_]{0,127}$/ (see zod-schema.core.ts)."""
    s = raw.strip().upper().replace("-", "_")
    s = re.sub(r"[^A-Z0-9_]", "", s)
    if not s:
        return ""
    m = re.search(r"[A-Z]", s)
    if not m:
        return ""
    s = s[m.start() :]
    s = re.sub(r"[^A-Z0-9_]", "", s)
    if len(s) > 128:
        s = s[:128]
    if not s or not ("A" <= s[0] <= "Z"):
        return ""
    return s


def _secret_ref(env_id: str) -> dict:
    # OpenClaw schema requires `provider` (see SecretRef / openclaw config set --ref-provider default).
    nid = _normalize_env_secret_id(env_id)
    if not nid:
        raise ValueError(f"Invalid env secret id after normalization: {env_id!r}")
    return {"source": "env", "provider": "default", "id": nid}


def _sanitize_channel_env_secret_refs(data: dict) -> bool:
    """Fix existing channels.* env SecretRefs (hyphenated/lowercase ids, wrong provider case)."""
    modified = False
    channels = data.get("channels")
    if not isinstance(channels, dict):
        return False
    for ch_name, field in (("discord", "token"), ("telegram", "botToken")):
        c = channels.get(ch_name)
        if not isinstance(c, dict):
            continue
        ref = c.get(field)
        if not isinstance(ref, dict) or ref.get("source") != "env":
            continue
        new_ref = dict(ref)
        changed = False
        if isinstance(new_ref.get("id"), str):
            nid = _normalize_env_secret_id(new_ref["id"])
            if nid and nid != new_ref["id"]:
                new_ref["id"] = nid
                changed = True
        if isinstance(new_ref.get("provider"), str):
            pl = new_ref["provider"].strip().lower()
            if pl != new_ref["provider"]:
                new_ref["provider"] = pl
                changed = True
        if changed:
            c[field] = new_ref
            modified = True
    return modified


def _remove_legacy_integrations_key(data: dict) -> bool:
    """OpenClaw 2026.3.23+ rejects top-level 'integrations'. Compose injects
    OPS_CONTROLLER_TOKEN / DASHBOARD_AUTH_TOKEN into the gateway container env."""
    if "integrations" not in data:
        return False
    del data["integrations"]
    return True


def _inject_channel_secret_refs(data: dict) -> bool:
    """If env provides Discord/Telegram tokens, set channels.* to env-backed SecretRef."""
    modified = False
    channels = data.setdefault("channels", {})
    if not isinstance(channels, dict):
        return False

    discord_env_id = ""
    if os.environ.get("DISCORD_BOT_TOKEN", "").strip():
        discord_env_id = "DISCORD_BOT_TOKEN"
    elif os.environ.get("DISCORD_TOKEN", "").strip():
        discord_env_id = "DISCORD_TOKEN"

    if discord_env_id:
        cd = channels.setdefault("discord", {})
        if isinstance(cd, dict):
            ref = _secret_ref(discord_env_id)
            if cd.get("token") != ref:
                cd["token"] = ref
                modified = True

    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        ct = channels.setdefault("telegram", {})
        if isinstance(ct, dict):
            ref = _secret_ref("TELEGRAM_BOT_TOKEN")
            if ct.get("botToken") != ref:
                ct["botToken"] = ref
                modified = True

    return modified


def _merge_deny_builtin_browser_unless_opt_in(data: dict) -> bool:
    """Deny built-in `browser` (no Chrome in gateway). Use Tavily MCP + web_fetch for web. Opt-in: OPENCLAW_ALLOW_BUILTIN_BROWSER=1."""
    if os.environ.get("OPENCLAW_ALLOW_BUILTIN_BROWSER", "").strip() == "1":
        return False
    tools = data.get("tools")
    if not isinstance(tools, dict):
        return False
    modified = False
    deny = tools.get("deny")
    if not isinstance(deny, list):
        deny = []
        tools["deny"] = deny
        modified = True
    if "browser" not in deny:
        deny.append("browser")
        modified = True
    return modified


def _merge_plugin_allowlist(data: dict) -> bool:
    """Pin trusted local plugins explicitly so OpenClaw does not treat them as opportunistic discoveries."""
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        return False
    allow = plugins.get("allow")
    modified = False
    if not isinstance(allow, list):
        allow = []
        plugins["allow"] = allow
        modified = True
    if "openclaw-mcp-bridge" not in allow:
        allow.append("openclaw-mcp-bridge")
        modified = True
    return modified


def _merge_elevated_allow_webchat(data: dict) -> bool:
    """If OPENCLAW_ELEVATED_ALLOW_WEBCHAT=1, enable tools.elevated for webchat sessions."""
    if os.environ.get("OPENCLAW_ELEVATED_ALLOW_WEBCHAT", "").strip() != "1":
        return False
    modified = False
    tools = data.setdefault("tools", {})
    if not isinstance(tools, dict):
        return False
    elev = tools.setdefault("elevated", {})
    if not isinstance(elev, dict):
        return False
    allow_from = elev.setdefault("allowFrom", {})
    if not isinstance(allow_from, dict):
        allow_from = {}
        elev["allowFrom"] = allow_from
    if elev.get("enabled") is not True:
        elev["enabled"] = True
        modified = True
    if _set_elevated_allow_from_all(allow_from, "webchat"):
        modified = True
    return modified


def _merge_unrestricted_gateway_container(data: dict) -> bool:
    """If OPENCLAW_UNRESTRICTED_GATEWAY_CONTAINER=1, relax exec + elevated for the gateway container.

    OpenClaw still runs only inside the container boundary unless you mount host paths.
    Pair with overrides/openclaw-gateway-root.yml (user 0:0) if you need apt/system package installs.
    """
    if os.environ.get("OPENCLAW_UNRESTRICTED_GATEWAY_CONTAINER", "").strip() != "1":
        return False
    modified = False
    tools = data.setdefault("tools", {})
    if not isinstance(tools, dict):
        return False

    # Exec on gateway (this container), no approval allowlist gate (see OpenClaw exec docs).
    ex = tools.setdefault("exec", {})
    if isinstance(ex, dict):
        if ex.get("host") != "gateway":
            ex["host"] = "gateway"
            modified = True
        if ex.get("security") != "full":
            ex["security"] = "full"
            modified = True
        if ex.get("ask") != "off":
            ex["ask"] = "off"
            modified = True

    elev = tools.setdefault("elevated", {})
    if isinstance(elev, dict):
        allow_from = elev.setdefault("allowFrom", {})
        if not isinstance(allow_from, dict):
            allow_from = {}
            elev["allowFrom"] = allow_from
        if elev.get("enabled") is not True:
            elev["enabled"] = True
            modified = True
        for key in ("webchat", "discord"):
            if _set_elevated_allow_from_all(allow_from, key):
                modified = True

    agents = data.setdefault("agents", {})
    if isinstance(agents, dict):
        defaults = agents.setdefault("defaults", {})
        if isinstance(defaults, dict):
            if defaults.get("elevatedDefault") != "full":
                defaults["elevatedDefault"] = "full"
                modified = True

    return modified


def _merge_discord_guild_allowlist_from_env(data: dict) -> bool:
    """Reconcile channels.discord.guilds against OPENCLAW_DISCORD_GUILD_IDS.

    Adds guilds not yet present and removes any that are no longer in the env list,
    making the env var the authoritative source when it is set. Guilds that were
    manually added (not via this function) are removed if they are absent from the env.
    When the env var is unset, nothing is touched.
    """
    raw = os.environ.get("OPENCLAW_DISCORD_GUILD_IDS", "").strip()
    if not raw:
        return False
    ids = []
    for part in raw.split(","):
        p = part.strip()
        if p.isdigit():
            ids.append(p)
    if not ids:
        return False
    channels = data.setdefault("channels", {})
    if not isinstance(channels, dict):
        return False
    disc = channels.setdefault("discord", {})
    if not isinstance(disc, dict):
        return False
    guilds = disc.setdefault("guilds", {})
    if not isinstance(guilds, dict):
        guilds = {}
        disc["guilds"] = guilds
    modified = False
    # Add guilds not yet present.
    for gid in ids:
        if gid not in guilds:
            # Private-server default: respond without @mention; adjust in JSON if needed.
            guilds[gid] = {"requireMention": False}
            modified = True
    # Remove guilds no longer in the env list.
    stale = [gid for gid in list(guilds) if gid not in ids]
    for gid in stale:
        del guilds[gid]
        modified = True
    return modified


def _merge_discord_user_allowlist_from_env(data: dict) -> bool:
    """Set the users allowlist on env-managed guilds from OPENCLAW_DISCORD_USER_IDS.

    Only guilds listed in OPENCLAW_DISCORD_GUILD_IDS are touched; guilds that OpenClaw
    auto-discovered outside that list are left unchanged. This prevents user IDs from
    leaking into guilds the operator never explicitly allowed.
    When OPENCLAW_DISCORD_GUILD_IDS is unset, falls back to applying to all guilds.
    """
    raw = os.environ.get("OPENCLAW_DISCORD_USER_IDS", "").strip()
    if not raw:
        return False
    user_ids = []
    for part in raw.split(","):
        p = part.strip()
        if p.isdigit():
            user_ids.append(p)
    if not user_ids:
        return False

    # Determine which guilds are env-managed (empty set = apply to all).
    env_guild_ids: set[str] = set()
    raw_guilds = os.environ.get("OPENCLAW_DISCORD_GUILD_IDS", "").strip()
    if raw_guilds:
        for part in raw_guilds.split(","):
            p = part.strip()
            if p.isdigit():
                env_guild_ids.add(p)

    channels = data.get("channels")
    if not isinstance(channels, dict):
        return False
    disc = channels.get("discord")
    if not isinstance(disc, dict):
        return False
    guilds = disc.get("guilds")
    if not isinstance(guilds, dict) or not guilds:
        print(
            "merge_gateway_config: OPENCLAW_DISCORD_USER_IDS is set but no guild entries exist in "
            "channels.discord.guilds — user IDs not applied. Set OPENCLAW_DISCORD_GUILD_IDS to "
            "register guilds first, or add them manually to openclaw.json.",
            file=sys.stderr,
        )
        return False
    modified = False
    for gid, guild_cfg in guilds.items():
        if env_guild_ids and gid not in env_guild_ids:
            continue
        if not isinstance(guild_cfg, dict):
            continue
        if guild_cfg.get("users") != user_ids:
            guild_cfg["users"] = list(user_ids)
            modified = True
    return modified


def _ensure_openclaw_state_dirs(config_root: Path) -> None:
    """Bootstrap core state directories expected after a clean wipe."""
    required = [
        config_root / "agents" / "main" / "agent",
        config_root / "agents" / "main" / "sessions",
        config_root / "canvas",
        config_root / "cron",
        config_root / "devices",
        config_root / "identity",
        config_root / "logs",
        config_root / "memory",
    ]
    for path in required:
        path.mkdir(parents=True, exist_ok=True)


def _gguf_model_entry(filename: str, context_window: int = LLAMACPP_CTX) -> dict:
    """Build an OpenClaw model dict from a bare GGUF filename."""
    lower = filename.lower()
    has_vision = "vision" in lower or "llava" in lower or "puppy" in lower
    is_embed = "embed" in lower or "embedding" in lower
    is_reasoning = (
        "r1" in lower or "reasoning" in lower
        or "qwen3" in lower  # Qwen3 family has built-in thinking mode
        or "gemma" in lower  # Gemma 4 family has thinking mode
    )
    name = filename.replace("-", " ").replace("_", " ").replace(".", " ")
    name = " ".join(w.capitalize() for w in name.split())
    return {
        "id": filename,
        "name": name,
        "reasoning": is_reasoning and not is_embed,
        "input": ["text", "image"] if has_vision else ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": min(context_window, 8192) if is_embed else context_window,
        "maxTokens": 8192,
    }


def _scan_gguf_models_from_disk() -> list[dict] | None:
    """Scan GGUF_MODELS_DIR for .gguf files.

    If LLAMACPP_MODEL is set, returns only that one file (the loaded model).
    Embed/embedding models are always excluded — they are not chat models.
    """
    gguf_dir = Path(os.environ.get("GGUF_MODELS_DIR", "/gguf-models"))
    if not gguf_dir.is_dir():
        return None
    active_model = os.environ.get("LLAMACPP_MODEL", "").strip()
    models = []
    try:
        for p in sorted(gguf_dir.iterdir()):
            if p.suffix.lower() != ".gguf" or not p.is_file():
                continue
            lower = p.name.lower()
            if "embed" in lower or "embedding" in lower:
                continue
            if active_model and p.name != active_model:
                continue
            models.append(_gguf_model_entry(p.name))
    except Exception as e:
        print(f"merge_gateway_config: could not scan GGUF dir {gguf_dir}: {e}", file=sys.stderr)
        return None
    return models if models else None


def _fetch_models_from_gateway() -> list[dict] | None:
    """Fetch /v1/models from model-gateway. Returns list of model dicts for OpenClaw, or None on failure."""
    try:
        req = urllib.request.Request(
            f"{MODEL_GATEWAY_URL.rstrip('/')}/v1/models",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"merge_gateway_config: could not fetch models from gateway: {e}", file=sys.stderr)
        return None

    items = data.get("data") if isinstance(data, dict) else []
    if not items:
        return None

    models = []
    for m in items:
        mid = m.get("id") or m.get("name", "")
        if not mid:
            continue
        # Skip ollama/-prefixed duplicates; the bare ID routes fine through the gateway
        if mid.startswith("ollama/"):
            continue
        # Skip :chat profile aliases — these are UI convenience aliases for Open WebUI,
        # not separate models. OpenClaw should use the base model ID.
        if m.get("profile") == "chat":
            continue
        lower_id = mid.lower()
        has_vision = "vision" in lower_id or "llava" in lower_id or "puppy" in lower_id
        is_reasoning = (
            "r1" in lower_id or "reasoning" in lower_id
            or "qwen3" in lower_id
            or "gemma" in lower_id
        )
        name = mid.split("/")[-1] if "/" in mid else mid
        name = name.replace(":", " ").replace("-", " ").replace(".", " ")
        name = " ".join(w.capitalize() for w in name.split())
        context_window = m.get("context_window")
        if not isinstance(context_window, int) or context_window <= 0:
            context_window = LLAMACPP_CTX
        models.append({
            "id": mid,
            "name": name,
            "reasoning": is_reasoning,
            "input": ["text", "image"] if has_vision else ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": context_window,
            "maxTokens": 8192,
        })
    return models if models else None


def _normalize_mcp_bridge_servers(data: dict) -> bool:
    """Ensure openclaw-mcp-bridge uses only the Docker MCP gateway (no separate comfyui URL)."""
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    try:
        import add_mcp_plugin_config as amp  # noqa: PLC0415

        return amp.normalize_mcp_bridge_servers(data)
    except Exception as e:
        print(f"merge_gateway_config: mcp bridge normalize skipped: {e}", file=sys.stderr)
        return False


def main() -> int:
    raw_path = os.environ.get("OPENCLAW_CONFIG_PATH", "/config/openclaw.json")
    config_path = Path(raw_path)
    if not config_path.exists() and raw_path == "/config/openclaw.json":
        host_candidate = _repo_root() / "data" / "openclaw" / "openclaw.json"
        if host_candidate.is_file():
            config_path = host_candidate
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"merge_gateway_config: skip (read error): {e}", file=sys.stderr)
            return 0
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {}

    providers = data.setdefault("models", {}).setdefault("providers", {})
    modified = False
    msg = ""
    if _sanitize_channel_env_secret_refs(data):
        modified = True
    if _sanitize_elevated_allow_from_legacy_booleans(data):
        modified = True
    if _normalize_mcp_bridge_servers(data):
        modified = True
    did_strip_integrations = _remove_legacy_integrations_key(data)
    if did_strip_integrations:
        modified = True
    did_channel_refs = _inject_channel_secret_refs(data)
    if did_channel_refs:
        modified = True

    did_guild_allowlist = False
    if _merge_discord_guild_allowlist_from_env(data):
        modified = True
        did_guild_allowlist = True
    if _merge_discord_user_allowlist_from_env(data):
        modified = True

    did_unrestricted_container = False

    # Strip baseUrl/apiKey from model objects (OpenClaw 2026.2.x rejects them per-model)
    for pv in providers.values() if isinstance(providers, dict) else []:
        if isinstance(pv, dict):
            for m in (pv.get("models") or []):
                if isinstance(m, dict) and ("baseUrl" in m or "apiKey" in m):
                    m.pop("baseUrl", None)
                    m.pop("apiKey", None)
                    modified = True

    # Remove direct ollama provider — all models route through the gateway
    if "ollama" in providers:
        del providers["ollama"]
        modified = True
        msg = "removed direct ollama provider (all models via gateway)"

    # Prefer disk scan (all GGUFs) over gateway fetch (only loaded model).
    # Fall back to gateway, then static defaults.
    disk_models = _scan_gguf_models_from_disk()
    if disk_models:
        gateway_models = disk_models
        msg = f"synced {len(gateway_models)} models from GGUF disk scan"
    else:
        gateway_models = _fetch_models_from_gateway()
        if gateway_models:
            msg = f"synced {len(gateway_models)} models from model-gateway"
        else:
            gateway_models = [m.copy() for m in DEFAULT_GATEWAY_MODELS]
            msg = f"using {len(gateway_models)} default models (gateway unreachable)"

    if "gateway" not in providers:
        providers["gateway"] = {
            **GATEWAY_PROVIDER,
            "models": gateway_models,
        }
        modified = True
    else:
        gw = providers["gateway"]
        if isinstance(gw, dict):
            if gw.get("api") != GATEWAY_PROVIDER["api"]:
                gw["api"] = GATEWAY_PROVIDER["api"]
                modified = True
            # Always sync from gateway (all Ollama models)
            gw["models"] = gateway_models
            modified = True
            gw.setdefault("headers", {})
            if not isinstance(gw["headers"], dict):
                gw["headers"] = {}
            if gw["headers"].get("X-Service-Name") != "openclaw":
                gw["headers"]["X-Service-Name"] = "openclaw"
                modified = True

    # Inject gateway auth token from env so it lives in .env, not in committed config
    gateway = data.setdefault("gateway", {})
    if gateway.get("mode") != "local":
        gateway["mode"] = "local"
        modified = True
    auth = gateway.setdefault("auth", {})
    if isinstance(auth, dict):
        token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
        if token and auth.get("token") != token:
            auth["token"] = token
            auth["mode"] = "token"
            modified = True
            msg = "injected gateway token from OPENCLAW_GATEWAY_TOKEN"

    # Bind to all interfaces so Docker port mapping works (container runs in isolation)
    if gateway.get("bind") != "lan":
        gateway["bind"] = "lan"
        modified = True

    # Use "default" compaction mode — "disabled" is not accepted by OpenClaw.
    # "default" compacts only when needed; "safeguard" compacts proactively.
    # Keeping "default" avoids the persistent-summary issue while staying valid.
    agents_defaults = data.setdefault("agents", {}).setdefault("defaults", {})
    compaction = agents_defaults.setdefault("compaction", {})
    if compaction.get("mode") != "default":
        compaction["mode"] = "default"
        modified = True
    # Ensure cron/isolated sessions have enough runway for Tavily + LLM generation.
    if agents_defaults.get("timeoutSeconds") != 300:
        agents_defaults["timeoutSeconds"] = 300
        modified = True
    # Disable idle-token timeout — large MoE/reasoning models can pause many seconds during
    # prompt prefill before the first token streams. Default 60 s kills valid generations.
    # Set to 0 to rely on timeoutSeconds only.
    llm_defaults = agents_defaults.setdefault("llm", {})
    if llm_defaults.get("idleTimeoutSeconds") != 0:
        llm_defaults["idleTimeoutSeconds"] = 0
        modified = True

    # Native web_search (Brave, etc.): keep disabled — use MCP gateway__call + duckduckgo__search.
    # Opt-in: OPENCLAW_NATIVE_WEB_SEARCH=1 skips forcing false (configure provider + keys per OpenClaw docs).
    if os.environ.get("OPENCLAW_NATIVE_WEB_SEARCH", "").strip() != "1":
        tools = data.setdefault("tools", {})
        if isinstance(tools, dict):
            web = tools.setdefault("web", {})
            if isinstance(web, dict):
                search = web.setdefault("search", {})
                if isinstance(search, dict) and search.get("enabled") is not False:
                    search["enabled"] = False
                    modified = True

    # Built-in `browser` requires Chrome in the gateway — unavailable in default Docker. Use Tavily MCP + web_fetch instead.
    if _merge_deny_builtin_browser_unless_opt_in(data):
        modified = True
    if _merge_plugin_allowlist(data):
        modified = True

    # Opt-in: full exec + elevated in gateway container (security-sensitive). Supersedes webchat-only.
    if _merge_unrestricted_gateway_container(data):
        modified = True
        did_unrestricted_container = True
    elif _merge_elevated_allow_webchat(data):
        modified = True

    # Disable device pairing (not needed in Docker — token auth is sufficient)
    control_ui = gateway.setdefault("controlUi", {})
    if isinstance(control_ui, dict):
        if not control_ui.get("dangerouslyDisableDeviceAuth"):
            control_ui["dangerouslyDisableDeviceAuth"] = True
            modified = True
        # Ensure dangerouslyAllowHostHeaderOriginFallback is set so any
        # LAN/Tailscale IP works without enumerating every address.
        if not control_ui.get("dangerouslyAllowHostHeaderOriginFallback"):
            control_ui["dangerouslyAllowHostHeaderOriginFallback"] = True
            modified = True
        # Gateway :6680 serves the Control UI HTML; :6682 inside the container is the browser/CDP helper.
        # Include both in allowedOrigins so the SPA can call the gateway without CORS errors.
        lan_ip = os.environ.get("LAN_IP", "192.0.2.1")
        required_origins = {
            "http://localhost:6680", "http://127.0.0.1:6680", f"http://{lan_ip}:6680",
            "http://localhost:6682", "http://127.0.0.1:6682", f"http://{lan_ip}:6682",
        }
        existing = set(control_ui.get("allowedOrigins") or [])
        if not required_origins.issubset(existing):
            control_ui["allowedOrigins"] = sorted(required_origins | existing)
            modified = True

    # Docker image: gateway binary is baked in — Control UI "Update" (npm/git) cannot replace it and often stalls.
    # Disable in-app update checks/auto-apply unless opted in via OPENCLAW_ALLOW_IN_APP_UPDATE=1.
    # Upgrade OpenClaw: docker compose pull && docker compose up -d openclaw-gateway (see openclaw/README.md).
    if os.environ.get("OPENCLAW_ALLOW_IN_APP_UPDATE", "").strip() != "1":
        upd = data.setdefault("update", {})
        if isinstance(upd, dict):
            if upd.get("checkOnStart") is not False:
                upd["checkOnStart"] = False
                modified = True
            auto = upd.setdefault("auto", {})
            if isinstance(auto, dict) and auto.get("enabled") is not False:
                auto["enabled"] = False
                modified = True

    _ensure_openclaw_state_dirs(config_path.parent)

    # Drift detection: warn if unrestricted settings are present but the flag is not set.
    # These settings are only written by _merge_unrestricted_gateway_container(), but they
    # persist in openclaw.json after the env var is removed (the function only escalates).
    if os.environ.get("OPENCLAW_UNRESTRICTED_GATEWAY_CONTAINER", "").strip() != "1":
        drifted: list[str] = []
        _tools = data.get("tools") or {}
        if isinstance(_tools, dict) and (_tools.get("exec") or {}).get("ask") == "off":
            drifted.append("tools.exec.ask=off")
        _agents = data.get("agents") or {}
        _defaults = (_agents.get("defaults") or {}) if isinstance(_agents, dict) else {}
        if isinstance(_defaults, dict) and _defaults.get("elevatedDefault") == "full":
            drifted.append("agents.defaults.elevatedDefault=full")
        if drifted:
            print(
                f"merge_gateway_config: WARNING: unrestricted settings detected in openclaw.json"
                f" ({', '.join(drifted)}) but OPENCLAW_UNRESTRICTED_GATEWAY_CONTAINER is not set."
                " These settings persist from a prior run. To restore safe defaults, set the flag,"
                " run config-sync, then clear the flag and manually revert those keys in openclaw.json.",
                file=sys.stderr,
            )

    if not modified:
        return 0
    try:
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        summary_parts: list[str] = []
        if did_strip_integrations:
            summary_parts.append("removed legacy integrations key")
        if did_channel_refs:
            summary_parts.append("channel SecretRefs from env")
        if did_guild_allowlist:
            summary_parts.append("Discord guild allowlist from OPENCLAW_DISCORD_GUILD_IDS")
        if did_unrestricted_container:
            summary_parts.append("unrestricted gateway container (OPENCLAW_UNRESTRICTED_GATEWAY_CONTAINER)")
        if msg:
            summary_parts.append(msg)
        summary = "; ".join(summary_parts) if summary_parts else "updated"
        print(f"merge_gateway_config: {summary} in openclaw.json")
    except OSError as e:
        print(f"merge_gateway_config: write failed: {e}", file=sys.stderr)
        return 1

    # Pre-create auth-profiles.json for the default agent so OpenClaw finds the
    # gateway API key immediately on first boot (avoids startup race).
    # Format: { "version": 1, "profiles": { "<id>": { "provider": "...", "type": "api_key", "key": "..." } } }
    agent_dir = config_path.parent / "agents" / "main" / "agent"
    auth_path = agent_dir / "auth-profiles.json"
    api_key = GATEWAY_PROVIDER["apiKey"]
    try:
        agent_dir.mkdir(parents=True, exist_ok=True)
        existing = {}
        if auth_path.exists():
            try:
                existing = json.loads(auth_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        profiles = existing.get("profiles", {}) if isinstance(existing, dict) else {}
        # Check if a valid gateway profile already exists
        has_gateway = any(
            p.get("provider") == "gateway" and p.get("key") == api_key
            for p in profiles.values() if isinstance(p, dict)
        )
        if not has_gateway:
            profiles["gateway-local"] = {
                "provider": "gateway",
                "type": "api_key",
                "key": api_key,
            }
            auth_path.write_text(json.dumps({"version": 1, "profiles": profiles}, indent=2), encoding="utf-8")
            print("merge_gateway_config: wrote auth-profiles.json for default agent")
    except OSError as e:
        print(f"merge_gateway_config: auth-profiles.json write skipped: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
