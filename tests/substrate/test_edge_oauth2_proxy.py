"""oauth2-proxy login must survive tabs that keep polling while signed out.

With one shared CSRF cookie, every unauthenticated poll from an open tab (the dashboards poll
their APIs every few seconds) starts a new login and overwrites the cookie. When Google redirects
back, the cookie belongs to a newer attempt and the callback fails with "CSRF token mismatch"
(observed 2026-09-24 signing in from a second computer). Per-request CSRF cookies, capped and
short-lived, keep each login attempt's token intact.
"""
from pathlib import Path

from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]


def _oauth2_proxy_command() -> list[str]:
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": ["edge"],
                            "site": {"BASE_PATH": "/srv/ordo", "DATA_PATH": "/srv/ordo/data",
                                     "CADDY_BIND": "127.0.0.1", "CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
                                     "CADDY_TAILNET_DOMAIN": "example.ts.net"}})
    rc = render(src, Catalog.load(ROOT / "catalog" / "models.yaml"), PluginRegistry.load(ROOT / "services"))
    return rc.compose_dict()["services"]["oauth2-proxy"]["command"]


def test_each_login_attempt_gets_its_own_csrf_cookie():
    assert "--cookie-csrf-per-request=true" in _oauth2_proxy_command()


def test_per_request_csrf_cookies_are_capped_and_short_lived():
    command = _oauth2_proxy_command()
    assert any(arg.startswith("--cookie-csrf-per-request-limit=") for arg in command)
    assert any(arg.startswith("--cookie-csrf-expire=") for arg in command)
