"""ops-controller's `GET /metrics`: what the control plane sees, in the Prometheus text format.

The monitoring plugin's Prometheus scrapes this and its alert rules
(monitoring/prometheus/rules/ordo-alerts.yml) read it. ops-controller is the one service that
already sees what these alerts need: the GPU lease and eviction state lives in its scheduler, it
holds the Docker socket (container state, health and restart counts), its root filesystem is the
Docker VM's disk and its /config bind is the host disk, and with the edge enabled it mounts the
edge's TLS certificate read-only. Exporting from here adds no privileged container to the stack.

Nothing here is a secret: service names, states, counts, byte sizes, lease ids and a certificate's
expiry time. The route is unauthenticated for that reason (Prometheus scrapes without a token),
the same as /health.

`render()` is pure (plain data in, text out) so it is tested without a scheduler, docker or a
filesystem; `ControlPlane.metrics_text()` (ordo/control/api.py) gathers the inputs.
"""
from __future__ import annotations

import dataclasses
import os
import ssl
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# The filesystems ops-controller reports, by the `mount` label the alert rules use.
#   docker: the container's own root, which lives on the Docker data disk (on Docker Desktop, the
#           WSL2 VM disk that filled to 100% on 2026-09-28).
#   host:   the /config bind (the checkout's out/ directory), which is on the host's disk.
DOCKER_DISK = "docker"
HOST_DISK = "host"


@dataclasses.dataclass(frozen=True)
class DiskUsage:
    """One filesystem's size, as statvfs reports it, in bytes. `free` includes the blocks reserved
    for root; `avail` is what an unprivileged writer can still use (df's "Avail")."""
    size: int
    free: int
    avail: int

    @classmethod
    def of(cls, path: str) -> DiskUsage:
        st = os.statvfs(path)
        return cls(size=st.f_blocks * st.f_frsize, free=st.f_bfree * st.f_frsize, avail=st.f_bavail * st.f_frsize)


@dataclasses.dataclass(frozen=True)
class Inputs:
    """Everything one scrape reports. None for a source that could not be read: its collector is
    reported as failed (`ordo_metrics_collector_ok 0`) and its series are left out, never zeroed,
    so an unreadable source cannot look like a healthy one."""
    scheduler: Mapping[str, Any] | None = None
    containers: Sequence[Mapping[str, Any]] | None = None     # list_services() rows
    restarts: Mapping[str, int] | None = None                 # service -> docker RestartCount
    disks: Mapping[str, DiskUsage | None] = dataclasses.field(default_factory=dict)
    # cert name -> its notAfter as a Unix time; None when a configured cert could not be read.
    tls_certs: Mapping[str, float | None] = dataclasses.field(default_factory=dict)


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    """Exact for whole numbers (a byte count past 1e12 must not round), repr for the rest."""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def _series(name: str, labels: Mapping[str, Any], value: float) -> str:
    inner = ",".join(f'{key}="{_escape(val)}"' for key, val in labels.items())
    return f"{name}{{{inner}}} {_number(value)}" if inner else f"{name} {_number(value)}"


class _Writer:
    """Collects one HELP/TYPE header per metric followed by its samples, in the order written."""

    def __init__(self) -> None:
        self._families: dict[str, list[str]] = {}

    def add(self, name: str, kind: str, help_text: str, labels: Mapping[str, Any], value: float) -> None:
        family = self._families.setdefault(name, [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"])
        family.append(_series(name, labels, value))

    def declare(self, name: str, kind: str, help_text: str) -> None:
        """A family with no samples yet still gets its header (an empty lease list is a fact)."""
        self._families.setdefault(name, [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"])

    def text(self) -> str:
        return "\n".join(line for family in self._families.values() for line in family) + "\n"


def _scheduler(w: _Writer, status: Mapping[str, Any]) -> None:
    running = list(status.get("running") or [])
    queued = list(status.get("queued") or [])
    w.add("ordo_gpu_leases_running", "gauge", "GPU leases currently admitted.", {}, len(running))
    w.add("ordo_gpu_leases_queued", "gauge", "GPU lease requests waiting for VRAM.", {}, len(queued))
    w.declare("ordo_gpu_lease_held_seconds", "gauge", "How long each running GPU lease has been held.")
    w.declare("ordo_gpu_lease_ttl_remaining_seconds", "gauge",
              "Seconds until each running lease's TTL expires (0: past its TTL, the sweep should "
              "have completed it).")
    for lease in running:
        labels = {"lease": lease.get("id", ""), "kind": lease.get("kind", "")}
        w.add("ordo_gpu_lease_held_seconds", "gauge", "", labels, float(lease.get("held_s") or 0.0))
        w.add("ordo_gpu_lease_ttl_remaining_seconds", "gauge", "", labels, float(lease.get("lease_ttl_s") or 0.0))
    # One series per registered resident, 0 while resident and 1 while stopped for a lease, so an
    # eviction is a value change on a series that always exists (a `for:` can time it).
    w.declare("ordo_gpu_resident_evicted", "gauge",
              "1 while a GPU resident is stopped to free VRAM for a lease, 0 while it is resident.")
    for resident in sorted(status.get("idle_cached") or {}):
        w.add("ordo_gpu_resident_evicted", "gauge", "", {"resident": resident}, 0)
    for resident in sorted(status.get("evicted_residents") or {}):
        w.add("ordo_gpu_resident_evicted", "gauge", "", {"resident": resident}, 1)


def _containers(w: _Writer, rows: Iterable[Mapping[str, Any]]) -> None:
    w.declare("ordo_container_running", "gauge", "1 while the service's container is running.")
    w.declare("ordo_container_unhealthy", "gauge",
              "1 while the service's container healthcheck reports unhealthy (0 otherwise, including "
              "a container with no healthcheck).")
    for row in rows:
        labels = {"service": row.get("id", "")}
        w.add("ordo_container_running", "gauge", "", labels, 1 if row.get("state") == "running" else 0)
        w.add("ordo_container_unhealthy", "gauge", "", labels, 1 if row.get("health") == "unhealthy" else 0)


def _restarts(w: _Writer, restarts: Mapping[str, int]) -> None:
    w.declare("ordo_container_restarts_total", "counter",
              "Docker's restart-policy restarts of the service's current container (a recreate starts "
              "a new container at 0).")
    for service in sorted(restarts):
        w.add("ordo_container_restarts_total", "counter", "", {"service": service}, int(restarts[service]))


def _disks(w: _Writer, disks: Mapping[str, DiskUsage]) -> None:
    for mount in sorted(disks):
        usage = disks[mount]
        labels = {"mount": mount}
        w.add("ordo_filesystem_size_bytes", "gauge", "Filesystem size in bytes.", labels, usage.size)
        w.add("ordo_filesystem_free_bytes", "gauge", "Free bytes, including those reserved for root.",
              labels, usage.free)
        w.add("ordo_filesystem_avail_bytes", "gauge", "Bytes an unprivileged writer can still use.",
              labels, usage.avail)


def _certs(w: _Writer, certs: Mapping[str, float]) -> None:
    for name in sorted(certs):
        w.add("ordo_tls_cert_not_after_timestamp_seconds", "gauge",
              "When the certificate expires, as a Unix time.", {"cert": name}, certs[name])


def render(inputs: Inputs) -> str:
    """The exposition text for one scrape."""
    w = _Writer()
    collectors: dict[str, bool] = {}
    if inputs.scheduler is not None:
        _scheduler(w, inputs.scheduler)
    collectors["containers"] = inputs.containers is not None
    if inputs.containers is not None:
        _containers(w, inputs.containers)
    collectors["restarts"] = inputs.restarts is not None
    if inputs.restarts is not None:
        _restarts(w, inputs.restarts)
    readable_disks = {mount: usage for mount, usage in inputs.disks.items() if usage is not None}
    for mount in inputs.disks:
        collectors[f"disk_{mount}"] = mount in readable_disks
    _disks(w, readable_disks)
    readable_certs = {name: when for name, when in inputs.tls_certs.items() if when is not None}
    for name in inputs.tls_certs:
        collectors[f"tls_cert_{name}"] = name in readable_certs
    _certs(w, readable_certs)
    for collector in sorted(collectors):
        w.add("ordo_metrics_collector_ok", "gauge",
              "1 when this scrape could read the collector's source, 0 when it could not.",
              {"collector": collector}, 1 if collectors[collector] else 0)
    return w.text()


def read_cert_not_after(path: str) -> float:
    """A PEM certificate's notAfter as a Unix time, read with the openssl CLI (the Python standard
    library has no public certificate parser). Raises OSError or ValueError when it cannot be read."""
    try:
        proc = subprocess.run(["openssl", "x509", "-enddate", "-noout", "-in", path],
                              capture_output=True, text=True, timeout=10)
    except subprocess.SubprocessError as e:
        raise OSError(f"openssl could not read {path}: {e}") from e
    line = proc.stdout.strip()
    if proc.returncode != 0 or not line.startswith("notAfter="):
        raise ValueError(f"openssl could not read {path}: {(proc.stderr or line).strip()[:200]}")
    # ssl.cert_time_to_seconds parses exactly openssl's "Dec 22 23:18:03 2026 GMT" form.
    return float(ssl.cert_time_to_seconds(line.removeprefix("notAfter=")))
