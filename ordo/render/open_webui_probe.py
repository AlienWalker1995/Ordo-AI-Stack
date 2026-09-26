"""Open WebUI's post-apply probe: does its declared connection authenticate against model-gateway?

Both the host's `ordo doctor` (after `ordo apply`) and ops-controller's post-render apply
(`ControlPlane.apply_render`, whenever it recreates open-webui) run this same probe inside the
open-webui container and judge its report with `open_webui_verdict`. It lives in the render layer
so the control plane can use it without importing the host.

Open WebUI's own /api/models needs a user session, so the check asks the question one level down,
with exactly what Open WebUI uses (ENABLE_PERSISTENT_CONFIG=false makes that its env): can the
scoped key in the running container list the gateway's models, and does that list hold the default
chat model and the embedding model? It runs inside the container, so the key never leaves it; only
status codes and model ids are printed.
"""
from __future__ import annotations

OPEN_WEBUI_SERVICE = "open-webui"
OPEN_WEBUI_PROBE = """
import json, os, urllib.error, urllib.request

def list_models(base_env, key_env):
    url = os.environ.get(base_env, '').rstrip('/') + '/models'
    headers = {'Authorization': 'Bearer ' + os.environ.get(key_env, '')}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            return {'status': response.status, 'models': [m.get('id') for m in json.load(response).get('data', [])]}
    except urllib.error.HTTPError as e:
        return {'status': e.code, 'models': []}
    except Exception as e:
        return {'status': 0, 'error': type(e).__name__ + ': ' + str(e)[:200], 'models': []}

report = {
    'persistent_config': os.environ.get('ENABLE_PERSISTENT_CONFIG', 'true'),
    'default_model': os.environ.get('DEFAULT_MODELS', ''),
    'embed_model': os.environ.get('RAG_EMBEDDING_MODEL', ''),
    'chat': list_models('OPENAI_API_BASE_URL', 'OPENAI_API_KEY'),
    'rag': list_models('RAG_OPENAI_API_BASE_URL', 'RAG_OPENAI_API_KEY'),
}
print(json.dumps(report))
"""


def open_webui_verdict(probe: dict | None) -> tuple[bool, str]:
    """(ok, one-line report) for an open-webui probe report (None: not running)."""
    if probe is None:
        return True, "open-webui: not running"
    problems = []
    if str(probe.get("persistent_config", "")).lower() != "false":
        problems.append("ENABLE_PERSISTENT_CONFIG is not false, so webui.db (not the render) holds its "
                        "connection; `ordo apply --only open-webui`")
    wanted = {"chat": ("default chat model", str(probe.get("default_model") or "")),
              "rag": ("embedding model", str(probe.get("embed_model") or ""))}
    for connection, (role, model) in wanted.items():
        result = probe.get(connection) or {}
        status = result.get("status")
        if status != 200:
            detail = result.get("error") or f"HTTP {status}"
            problems.append(f"model-gateway refuses the {connection} connection's scoped key ({detail}); "
                            f"check LITELLM_KEY_OPEN_WEBUI, then `ordo apply --only open-webui`")
        elif model not in (result.get("models") or []):
            available = ", ".join(result.get("models") or []) or "none"
            problems.append(f"the {role} {model!r} is not among the models its key may use ({available})")
    if problems:
        return False, "! open-webui: " + "; ".join(problems)
    return True, (f"open-webui: env-authoritative config; its scoped key lists "
                  f"{probe['default_model']} and {probe['embed_model']} on model-gateway")
