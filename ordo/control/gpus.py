"""The GPU as the control plane shows and leases it.

- `LeaseJobs`: the lease routes (`POST /jobs`, `/jobs/complete`, `/jobs/heartbeat`) over the broker,
  each answering with the scheduler's status. None of them takes the operation lock: a heartbeat
  must answer while a recreate runs.
- The registry views: which models the render serves on which card (ordo/render/served_models.py,
  derived on every call, so there is no stored registry to drift from ordo.yaml), joined with the
  live cards (ordo/render/gpu_live.py).
- `assign_gone`: the retired runtime GPU reassignment, an honest 410.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..render import gpu_live
from ..render.served_models import models_by_gpu, served_models
from .broker import Broker
from .responses import error
from .scheduler import Job, Scheduler


class LeaseJobs:
    """Request, release and heartbeat a GPU lease through the broker."""

    def __init__(self, broker: Broker | None, scheduler: Scheduler | None):
        self.broker = broker
        self.scheduler = scheduler

    def request(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        try:
            job = Job(id=str(body["id"]), vram_gb=float(body["vram_gb"]),
                      kind=str(body.get("kind", "generic")),
                      est_seconds=float(body.get("est_seconds", 0.0)))
        except (KeyError, ValueError, TypeError):
            return error(400, "job needs 'id' and numeric 'vram_gb'")
        self.broker.request(job)
        return self.scheduler.status()

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        job_id = str(body.get("id", "")).strip()
        if not job_id:
            return error(400, "body must include 'id'")
        self.broker.complete(job_id)
        return self.scheduler.status()

    def heartbeat(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        job_id = str(body.get("id", "")).strip()
        if not job_id:
            return error(400, "body must include 'id'")
        if not self.broker.heartbeat(job_id):
            return error(404, f"no running job '{job_id}'")
        return self.scheduler.status()


def served_by(rendered: Any) -> dict[str, dict[str, Any]]:
    """Every model a render serves, keyed by model id."""
    return served_models(rendered.compose_dict(), rendered.env, rendered.gpu_inventory())


def live_cards() -> dict[str, Any]:
    """Every card's live VRAM, utilization and temperature (ordo/render/gpu_live.py)."""
    return {"gpus": gpu_live.live_gpus()}


def live_by_uuid() -> dict[str, dict[str, Any]]:
    """The live GPU reader's cards keyed by uuid, in the GiB units /registry/gpus has always used."""
    out: dict[str, dict[str, Any]] = {}
    for card in gpu_live.live_gpus():
        used_mib = card["vram_used_mib"]
        out[card["uuid"]] = {
            "name": card["name"],
            "total_gb": round(card["vram_total_mib"] / 1024.0, 1),
            "used_gb": round(used_mib / 1024.0, 1) if used_mib is not None else None,
            "util": card["utilization_pct"],
        }
    return out


def registry_gpus(live: dict[str, dict[str, Any]], served: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Live GPU info with the models the render pins to each card."""
    uuid_to_models = models_by_gpu(served)
    result: dict[str, Any] = {}
    for uuid, info in live.items():
        result[uuid] = {**info, "models": uuid_to_models.get(uuid, [])}
    return {"gpus": result}


def gpu_indexes() -> dict[str, str]:
    """nvidia-smi index -> uuid for every card on the host (ordo/render/gpu_live.py), so a device
    named by index resolves to one card. Empty when unreadable: an index then proves nothing."""
    try:
        return {str(card["index"]): str(card["uuid"]) for card in gpu_live.live_gpus()
                if card.get("uuid") and card.get("index") is not None}
    except Exception:  # noqa: BLE001 - unknown means managed.gpu_refusal fails closed on indexes
        return {}


def leased_gpu_uuid(render: Callable[[], Any]) -> str | None:
    """The uuid of the card the scheduler leases (the primary card of `render()`), or None when
    unknown."""
    try:
        gpu = render().hardware.primary_gpu
    except Exception:  # noqa: BLE001 - unknown means managed.gpu_refusal fails closed
        return None
    return getattr(gpu, "uuid", None) or None


def assign_gone() -> dict[str, Any]:
    """410 GONE. GPU pins are baked at `ordo render` time, not at runtime.

    The v1 flow wrote overrides/gpu-assignments.yml and recreated the service. Under the render
    substrate nothing reads that file back and a recreate replays the already-rendered compose
    byte for byte, so the endpoint answered {"ok": true} while changing nothing. This mirrors
    the /guardian/* retirement: an honest 410 beats a silent no-op.
    """
    return error(
        410,
        "GPU reassignment moved to the render pipeline: set the service's `gpu_pin:` in its "
        "manifest and re-render (`ordo render`), then recreate the service. "
        "Runtime reassignment was a silent no-op and has been retired.",
    )
