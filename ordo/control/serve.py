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

from ..render.catalog import Catalog
from ..render.config import Source
from ..render.engine import DEFAULT_PLUGINS_DIR, render
from ..render.hardware import detect
from ..render.models_volume import DockerRunner, volume_files
from ..render.plugins import PluginRegistry
from ..secret_env import SecretFileError, read_secret
from . import principals


def cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover - binds a socket
    import threading
    import time

    # The control plane's libraries (fastapi, uvicorn, pydantic, httpx) are the `serve` extra:
    # imported here so every other command runs on the PyYAML-only core.
    from .api import ControlPlane
    from .broker import Broker, DockerBackend
    from .residents import start_residents
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
    # GET /metrics disks: this container's root lives on the Docker data disk, and --out (/config)
    # is a bind of the host's out/ directory, so it reports the host disk.
    # The edge's certificate, when the render mounted it (EDGE_TLS_CERT_FILE, ordo/render/compose.py).
    from .metrics import DOCKER_DISK, HOST_DISK

    edge_cert = os.environ.get("EDGE_TLS_CERT_FILE", "").strip()
    cp = ControlPlane(Path(args.source), cat, reg, args.out, scheduler=sched, broker=broker,
                      history=history,
                      model_volume_files=lambda: volume_files(DockerRunner(), args.project),
                      disk_paths={DOCKER_DISK: "/", HOST_DISK: str(args.out)},
                      tls_cert_files={"edge": edge_cert} if edge_cert else {})

    # Resident registration from the render, then the previous process's lease state adopted BEFORE
    # the lease loop or the API run (a lease held across this restart keeps its resident evicted,
    # and one that expired while down is swept), then the adopted footprints reconciled with the
    # render (ordo/control/residents.py).
    start_residents(sched, broker, cp.residents, render(src, cat, reg) if hw.has_gpu else None)
    if state_store is not None:
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
                # A host `ordo apply` renders out/ and recreates services without calling this
                # process: adopt the new render's resident footprints within one tick.
                cp.residents.adopt_if_render_changed()
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
    # Hermes' scoped token (ordo/control/principals.py). Optional: a store without the key
    # materializes an empty file, and an empty token matches nothing.
    hermes = principals.hermes(lambda: read_secret("OPS_CONTROLLER_TOKEN_HERMES"))
    print(f"ops-controller: hermes principal {'active' if hermes.token.current() else 'off (no token)'}",
          flush=True)
    # Re-read on every request, so a rotated token file takes effect without a restart.
    cp.serve(lambda: read_secret("OPS_CONTROLLER_TOKEN"), host=args.host, port=args.port, scoped=[hermes])
    return 0
