"""Whether the alerts the monitoring plugin evaluates can reach anyone: the `ordo doctor` check.

Alertmanager (services/monitoring/plugin.yaml) delivers to two URLs held as file-delivered secrets:
the operator's Discord webhook, and the off-box heartbeat check the always-firing Watchdog alert
pings. Both are optional for a bring-up, so a fresh install is never blocked on them, and a blank
one materializes as an empty file: Alertmanager runs, and every delivery to that receiver fails.
That is the silent failure this check makes loud. `ordo doctor` (the host) and ops-controller's
GET /doctor (the dashboard's Overview) both judge it with `delivery_verdict`, never a copy.

Only the rendered compose decides whether the check applies: it applies when a service mounts one
of these keys (the monitoring plugin is enabled), so a stack without monitoring passes.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import secret_files

DISCORD_WEBHOOK_KEY = "ALERTMANAGER_DISCORD_WEBHOOK_URL"
HEARTBEAT_KEY = "ALERTMANAGER_HEARTBEAT_URL"
# What goes missing while each key is blank, for the doctor line.
DELIVERY_KEYS: dict[str, str] = {
    DISCORD_WEBHOOK_KEY: "alerts fire but notify no one",
    HEARTBEAT_KEY: "no off-box heartbeat, so a dead box goes unnoticed",
}
RUNBOOK = "docs/runbooks/alerting.md"


def rendered_delivery_keys(compose: Mapping[str, Any]) -> list[str]:
    """The delivery keys a service of the rendered compose mounts, in DELIVERY_KEYS order."""
    mounted = {key for _service, key in secret_files.secret_files_in(compose)}
    return [key for key in DELIVERY_KEYS if key in mounted]


def blank_keys(keys: list[str], out_dir: str | Path) -> list[str]:
    """Which of `keys` have an empty (or missing) materialized file under <out>/secrets/. Reads the
    files only to test them for content; a value is never returned or printed."""
    blank = []
    for key in keys:
        path = Path(out_dir) / "secrets" / key.lower()
        try:
            has_value = bool(path.read_text(encoding="utf-8").strip())
        except OSError:
            has_value = False
        if not has_value:
            blank.append(key)
    return blank


def delivery_verdict(rendered_keys: list[str], blank: list[str]) -> tuple[bool, str]:
    """(ok, one-line report). A finding line starts with "! ", like every doctor finding."""
    if not rendered_keys:
        return True, "alerting: monitoring is not enabled, so there is no alert delivery to check"
    if not blank:
        return True, "alerting: the Discord webhook and the heartbeat URL are set"
    effects = "; ".join(f"{key} is blank: {DELIVERY_KEYS[key]}" for key in blank)
    return False, (f"! alerting: {effects}. Set it with `ordo secrets set <KEY> --from-stdin` and run "
                   f"the recreate it prints ({RUNBOOK})")


def check(compose: Mapping[str, Any], out_dir: str | Path) -> tuple[bool, str]:
    """The whole check: which keys the render mounts, which of those are blank, the verdict."""
    keys = rendered_delivery_keys(compose)
    return delivery_verdict(keys, blank_keys(keys, out_dir))
