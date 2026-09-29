"""`GET /doctor`: the drift `ordo doctor` reports, seen from the control plane. Read-only.

Each check is judged by the function `ordo doctor` uses, never a copy: this process's substrate
digest against the one the last render recorded in out/manifest.json (`substrate.substrate_verdict`;
the host compares the running ops-controller with its checkout, which this process cannot see), the
open-webui probe (`open_webui_verdict`), run once in its container when it is running, and alert
delivery (`alerting.check`).
"""
from __future__ import annotations

from typing import Any

from ..render import alerting, substrate
from ..render.open_webui_probe import OPEN_WEBUI_SERVICE, open_webui_verdict
from .apply import RenderApply
from .broker import Broker
from .source import StackSource


class DriftReport:
    """The checks `ordo doctor` makes that this process can make too."""

    def __init__(self, source: StackSource, applier: RenderApply, broker: Broker | None):
        self.source = source
        self.applier = applier
        self.broker = broker

    def report(self) -> dict[str, Any]:
        """Every check with its verdict. `detail` is the CLI's report line without its "! " finding
        marker. The dashboard's Overview shows the failed checks."""
        checks = [("substrate", *self._substrate_drift()), (OPEN_WEBUI_SERVICE, *self._open_webui_drift()),
                  ("alerting", *self._alerting_drift())]
        rows = [{"check": name, "ok": ok, "detail": line.removeprefix("! ")} for name, ok, line in checks]
        return {"ok": all(row["ok"] for row in rows), "checks": rows}

    def _substrate_drift(self) -> tuple[bool, str]:
        digest = self.source.substrate_digest
        try:
            recorded = self.source.recorded_substrate_digest()
        except (OSError, ValueError, AttributeError) as e:
            return False, f"! substrate: cannot read {self.source.out_dir / 'manifest.json'} ({e})"
        if recorded is None:
            return True, f"substrate: ops-controller {digest[:12]}; out/ records no digest yet"
        return substrate.substrate_verdict(digest, recorded, reference_name="the last render",
                                           rebuild_from="the checkout that rendered out/")

    def _alerting_drift(self) -> tuple[bool, str]:
        """The alert-delivery verdict `ordo doctor` gives, from the secret files materialized in out/."""
        try:
            compose = self.source.render().compose_dict()
        except Exception as e:  # noqa: BLE001 - an unrenderable source is reported, not raised
            return False, f"! alerting: cannot render the source to check alert delivery ({type(e).__name__}: {e})"
        return alerting.check(compose, self.source.out_dir)

    def _open_webui_drift(self) -> tuple[bool, str]:
        """The open-webui verdict `ordo doctor` gives: "not running" is fine, a failed probe is not."""
        if not self.broker:
            return False, "! open-webui: cannot be checked: this control plane has no container backend"
        try:
            rows = self.broker.backend.list_services().get("services", [])
        except Exception as e:  # noqa: BLE001 - an unreadable stack is reported, not raised
            return False, f"! open-webui: cannot read the running services ({type(e).__name__}: {e})"
        running = any(row.get("id") == OPEN_WEBUI_SERVICE and row.get("state") == "running" for row in rows)
        return self.applier.open_webui_verdict() if running else open_webui_verdict(None)
