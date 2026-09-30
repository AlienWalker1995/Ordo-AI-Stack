"""The GPU residents the scheduler holds as reclaimable, and the VRAM each one holds.

Registration is DERIVED from the declared GPU inventory of the render (ordo/render/gpu.py), not from
a `--resident-service llamacpp` default: every service that declares it holds VRAM on the primary
device and may be reclaimed is registered as idle-cached, so a burst request can actually evict it.
An unregistered resident's VRAM looks free and the scheduler admits a job into space that is already
taken. Secondary-device residents (the 1070's voice models) are excluded on purpose, and a
non-preemptible resident's VRAM is removed from the budget instead of being offered.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

from ..render import gpu


def footprints(rc: Any) -> dict[str, float]:
    """{service: VRAM} for every reclaimable resident on the primary card, from a render."""
    return gpu.primary_residents(rc.gpu_inventory())


class ResidentFootprints:
    """Keeps the scheduler's resident footprints equal to what the current render declares.

    Registration used to happen once, at startup, so a model switch left the scheduler holding the
    old model's footprint until ops-controller restarted: after the 2026-09-30 switch it counted
    llama.cpp at 27.5GB while the new model held 30.2GB, and offered VRAM that was not free. Now the
    footprints are adopted again whenever the render changes:

      * after every control-plane apply (the dashboard's model switch, a plugin change), by
        RenderApply;
      * by the lease loop when the operator source or out/manifest.json changes on disk, which is
        how a host `ordo apply` (it renders out/ and recreates services without calling this
        process) reaches the scheduler, within one lease-loop tick;
      * at startup, after the persisted lease state is adopted (start_residents).
    """

    def __init__(self, source: Any, broker: Any):
        self.source = source          # ordo/control/source.py StackSource: .path, .out_dir, .render()
        self.broker = broker
        self._seen: str | None = None  # the render inputs last adopted

    def _fingerprint(self) -> str:
        """A digest of what a render reads that a host apply rewrites: the operator source and the
        manifest the last render wrote."""
        digest = hashlib.sha256()
        for path in (Path(self.source.path), Path(self.source.out_dir) / "manifest.json"):
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<missing>")
            digest.update(b"|")
        return digest.hexdigest()

    def adopt(self, rc: Any | None = None) -> dict[str, tuple[float, float]]:
        """Set the scheduler's footprints to those of `rc` (default: the render of the source on disk
        now). Returns {resident: (old, new)} for each one that changed."""
        if self.broker is None:
            return {}
        fingerprint = self._fingerprint()     # before rendering: a change during it is seen next time
        if rc is None:
            rc = self.source.render()
        changed = self.broker.adopt_resident_footprints(footprints(rc))
        self._seen = fingerprint
        return changed

    def adopt_if_render_changed(self) -> dict[str, tuple[float, float]] | None:
        """The lease loop's check: re-render and adopt only when the render inputs changed since the
        last adoption. None when nothing changed (no render), or when the render failed (the
        footprints stay as they are, and the failure is reported once)."""
        if self.broker is None:
            return None
        fingerprint = self._fingerprint()
        if fingerprint == self._seen:
            return None
        try:
            rc = self.source.render()
        except Exception as e:  # noqa: BLE001 - a bad source must not stop the lease loop
            self._seen = fingerprint
            print(f"[scheduler] cannot re-read the render to update resident footprints ({e}); "
                  "keeping the current ones", file=sys.stderr, flush=True)
            return None
        return self.adopt(rc)


def start_residents(scheduler: Any, broker: Any, residents: ResidentFootprints, rc: Any | None) -> None:
    """`ordo serve` startup, in this order: register the render's residents (`rc`, None on a host
    with no GPU), adopt the lease state the previous process persisted, then reconcile the adopted
    footprints with the render (a resident evicted before the restart was saved at the size it had
    then)."""
    if rc is not None:
        claims = rc.gpu_inventory()
        pinned = gpu.pinned_primary_vram_gb(claims)
        if pinned:
            scheduler.total_vram_gb = round(scheduler.total_vram_gb - pinned, 2)
            print(f"[scheduler] {pinned:.1f}GB of the primary card is held by non-preemptible "
                  f"residents; removed from the admission budget", flush=True)
        for service, vram in footprints(rc).items():
            scheduler.cache_idle(service, vram)
            print(f"[scheduler] resident '{service}' ~{vram:.1f}GB registered as reclaimable", flush=True)
        for c in claims:
            degraded = f" -> {c.degraded_service}" if c.degraded_service else ""
            print(f"[scheduler] gpu claim: {c.service:<16} mode={c.mode:<8} "
                  f"enforcement={c.enforcement:<7} device={c.device:<9} "
                  f"vram={c.vram_gb:>6.1f}GB yield={c.yield_strategy}{degraded}", flush=True)
    if broker is not None:
        broker.restore_state()
    if rc is not None:
        residents.adopt(rc)
