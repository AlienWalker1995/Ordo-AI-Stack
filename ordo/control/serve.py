"""`ordo serve`: the control-plane process (ops-controller's entrypoint).

Wires the scheduler, the broker and the lease history to the HTTP API (ordo/control/api.py), registers
the GPU residents the render declares, adopts the lease state a previous process saved, and runs the
lease-sweep thread. Argument parsing lives in ordo/cli.py, which imports this module only for `serve`,
so the control plane never loads the host tooling (ordo/host/).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..render import gpu
from ..render.catalog import Catalog
from ..render.config import Source
from ..render.engine import DEFAULT_PLUGINS_DIR, render
from ..render.hardware import detect
from ..render.models_volume import DockerRunner, volume_files
from ..render.plugins import PluginRegistry
from ..secret_env import SecretFileError, read_secret


def cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover - binds a socket
    import threading
    import time

    # The control plane's libraries (fastapi, uvicorn, pydantic, httpx) are the `serve` extra:
    # imported here so every other command runs on the PyYAML-only core.
    from .api import ControlPlane
    from .broker import Broker, DockerBackend
    from .scheduler import Scheduler

    cat = Catalog.load(Path(args.catalog))
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    src = Source.load(Path(args.source))
    hw = detect()
    sched = Scheduler(hw.primary_vram_gb if hw.has_gpu else 0.0)
    # Durable lease record (served at GET /jobs/history for the dashboard's orchestration tab).
    # Lives next to the rendered outputs — the same writable /config mount, no extra volume.
    from .lease_history import LeaseHistory

    history = LeaseHistory(Path(args.out) / "lease-history.jsonl")
    # The scheduler's live lease and eviction state, on the /data bind (SCHEDULER_STATE_PATH, set
    # by the rendered compose). Unset (a hand-run `ordo serve`) means it is kept in memory only.
    from .scheduler_state import SchedulerStateStore

    state_path = os.environ.get("SCHEDULER_STATE_PATH", "").strip()
    state_store = SchedulerStateStore(Path(state_path)) if state_path else None
    broker = Broker(sched, DockerBackend(project=args.project), history=history, state_store=state_store)
    cp = ControlPlane(Path(args.source), cat, reg, args.out, scheduler=sched, broker=broker,
                      history=history,
                      model_volume_files=lambda: volume_files(DockerRunner(), args.project))

    # Resident registration, DERIVED from the declared GPU inventory (ordo/render/gpu.py) rather than
    # from a `--resident-service llamacpp` default. Every service that DECLARES it holds VRAM on
    # the primary device and may be reclaimed is registered as idle-cached, so a burst request
    # can actually evict it; an unregistered resident's VRAM looks free and the scheduler admits
    # a job into space that is already taken (the live defect: /status showed free == total).
    # Secondary-device residents (the 1070's voice models) are excluded on purpose, and a
    # non-preemptible resident's VRAM is removed from the budget instead of being offered.
    # Read from the same render the stack runs, so it can't drift from what `.env` loads.
    if hw.has_gpu:
        rc = render(src, cat, reg)
        claims = rc.gpu_inventory()
        pinned = gpu.pinned_primary_vram_gb(claims)
        if pinned:
            sched.total_vram_gb = round(sched.total_vram_gb - pinned, 2)
            print(f"[scheduler] {pinned:.1f}GB of the primary card is held by non-preemptible "
                  f"residents — removed from the admission budget", flush=True)
        for service, vram in gpu.primary_residents(claims).items():
            sched.cache_idle(service, vram)
            print(f"[scheduler] resident '{service}' ~{vram:.1f}GB registered as reclaimable",
                  flush=True)
        for c in claims:
            degraded = f" -> {c.degraded_service}" if c.degraded_service else ""
            print(f"[scheduler] gpu claim: {c.service:<16} mode={c.mode:<8} "
                  f"enforcement={c.enforcement:<7} device={c.device:<9} "
                  f"vram={c.vram_gb:>6.1f}GB yield={c.yield_strategy}{degraded}", flush=True)

    # Adopt the previous process's lease state BEFORE the lease loop or the API run: a lease held
    # across this restart keeps its resident evicted, and one that expired while down is swept.
    if state_store is not None:
        broker.restore_state()
        print(f"[scheduler] lease state persisted at {state_path}; adopted "
              f"running={sched.running_ids} queued={sched.queued_ids} "
              f"evicted={sorted(sched.evicted_residents)}", flush=True)
    else:
        print("[scheduler] SCHEDULER_STATE_PATH is not set: the lease state is in memory only, and "
              "a restart mid-lease loses it", flush=True)

    # Lease clock + self-heal sweep: advance the scheduler's clock by the poll interval and force-
    # complete any lease whose TTL has elapsed (a crashed client can never strand the resident down).
    # sweep_leases() reconciles, which restores an evicted resident once the GPU work has drained.
    def _lease_loop() -> None:
        while True:
            time.sleep(args.lease_poll_seconds)
            try:
                sched.tick(args.lease_poll_seconds)
                swept = broker.sweep_leases()
                if swept:
                    print(f"[scheduler] lease TTL expired for {swept} — resident restored on drain",
                          flush=True)
                stray = broker.enforce_evictions()
                if stray:
                    print(f"[scheduler] ERROR: evicted resident(s) {stray} were running during a GPU "
                          f"lease (started outside the scheduler, e.g. a whole-stack compose up); "
                          f"stopped them again", flush=True)
            except Exception as e:  # noqa: BLE001 — the control plane must survive a sweep hiccup
                print(f"[scheduler] lease sweep error: {e}", flush=True)

    threading.Thread(target=_lease_loop, daemon=True, name="lease-sweep").start()

    try:
        # A file under /run/secrets (OPS_CONTROLLER_TOKEN_FILE, the rendered delivery), else the env var.
        token = read_secret("OPS_CONTROLLER_TOKEN")
    except SecretFileError as e:
        print(f"ops-controller: {e}; refusing to serve", file=sys.stderr, flush=True)
        return 2
    if not token:
        print("ops-controller: OPS_CONTROLLER_TOKEN is not set; refusing to serve an unauthenticated "
              "control plane (it is provisioned in the secret store)", file=sys.stderr, flush=True)
        return 2
    print(f"ops-controller on {args.host}:{args.port} (project={args.project}, "
          f"{sched.total_vram_gb:.0f}GB GPU) — Ctrl-C to stop")
    cp.serve(token, host=args.host, port=args.port)
    return 0
