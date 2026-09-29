"""Maintenance of OTHER compose projects on this host: status, logs and a confirmed restart.

The operator lists the projects Hermes may maintain as `managed_projects:` in ordo.yaml (the
source of truth; `Source.validate` refuses Ordo's own project). ops-controller serves them under
`/projects/...` (ordo/control/api.py). This module holds the pure policy those routes apply:

- `gpu_refusal`: a restart must never put a second tenant on the GPU the scheduler leases. Ordo's
  lease-managed residents are out of reach by construction (Ordo's project cannot be listed); this
  guard covers a FOREIGN container whose device requests or NVIDIA_VISIBLE_DEVICES expose the
  leased card, unless CUDA_VISIBLE_DEVICES pins it elsewhere (the one rule is in that function).
- `RestartBudget`: at most `limit` restarts per container per sliding window (3 per hour), so a
  confused agent cannot flap a service; the next one is refused with 429 and a retry time.

Docker access itself stays in the broker's backend (ordo/control/broker.py).
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping

# A compose project name: lowercase letters, digits, dashes and underscores, starting with a
# letter or digit (compose's own rule). It also keeps a project name safe to put in a path.
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# A container name as docker allows it; a path segment never carries a slash.
CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# The most log lines one call returns.
LOG_TAIL_MAX = 2000
LOG_TAIL_DEFAULT = 100
# The only fields a project container row carries (the backend builds them from `docker ps`).
ROW_FIELDS = ("name", "service", "state", "health", "status", "image")
RESTARTS_PER_WINDOW = 3
RESTART_WINDOW_SECONDS = 3600

def _env_value(raw: dict, key: str) -> str | None:
    for entry in (raw.get("Config") or {}).get("Env") or []:
        name, sep, value = str(entry).partition("=")
        if sep and name == key:
            return value.strip()
    return None


def _requests_gpu(raw: dict) -> bool:
    host_config = raw.get("HostConfig") or {}
    if str(host_config.get("Runtime") or "") == "nvidia":
        return True
    for request in host_config.get("DeviceRequests") or []:
        capabilities = [c for group in request.get("Capabilities") or [] for c in group]
        if request.get("Driver") == "nvidia" or "gpu" in capabilities:
            return True
    return False


# A device list that names every card: "all", a bare count, or a device nobody can identify.
_ANY_CARD = None


def _resolve(devices: list[str], gpu_indexes: Mapping[str, str]) -> set[str] | None:
    """Device tokens (uuids or nvidia-smi indexes) as lower-case uuids, or None when any token
    cannot be resolved to one card ("all", an index the inventory lacks, a MIG id...)."""
    uuids: set[str] = set()
    for token in (d.strip() for d in devices):
        if not token:
            continue
        if token.lower().startswith("gpu-"):
            uuids.add(token.lower())
        elif token in gpu_indexes:
            uuids.add(gpu_indexes[token].lower())
        else:
            return _ANY_CARD
    return uuids


def _env_devices(raw: dict, key: str, gpu_indexes: Mapping[str, str]) -> set[str] | None:
    """The cards an env pin names: None for `all`, empty for `none`/`void`, unset returns None
    too (the caller decides what unset means)."""
    value = _env_value(raw, key)
    if value is None or value.lower() == "all":
        return _ANY_CARD
    if value.lower() in ("none", "void", ""):
        return set()
    return _resolve(value.split(","), gpu_indexes)


def _exposed_devices(raw: dict, gpu_indexes: Mapping[str, str]) -> set[str] | None:
    """Every card the container is given, from its device requests AND NVIDIA_VISIBLE_DEVICES
    (the union: either one can hand it a card). None means any card."""
    host_config = raw.get("HostConfig") or {}
    exposed: set[str] = set()
    requests = host_config.get("DeviceRequests") or []
    for request in requests:
        device_ids = request.get("DeviceIDs") or []
        if not device_ids:
            return _ANY_CARD                       # a count (or -1, all): docker picks the cards
        resolved = _resolve([str(d) for d in device_ids], gpu_indexes)
        if resolved is _ANY_CARD:
            return _ANY_CARD
        exposed |= resolved
    if _env_value(raw, "NVIDIA_VISIBLE_DEVICES") is not None or not requests:
        # Set, it can widen what the requests give; unset with only the nvidia runtime, the
        # runtime's default is every card.
        from_env = _env_devices(raw, "NVIDIA_VISIBLE_DEVICES", gpu_indexes)
        if from_env is _ANY_CARD:
            return _ANY_CARD
        exposed |= from_env
    return exposed


def gpu_refusal(raw: dict, leased_uuid: str | None, gpu_indexes: Mapping[str, str]) -> str | None:
    """Why restarting this container could touch the leased GPU, or None when it cannot.

    The one rule: refuse when the container's device requests or NVIDIA_VISIBLE_DEVICES expose the
    leased card (by uuid, by index, as `all`, or as a bare count that lets docker pick), UNLESS
    CUDA_VISIBLE_DEVICES pins it to other cards only. On Docker Desktop/WSL2 CUDA_VISIBLE_DEVICES
    is the only pin that isolates a process (device ids and NVIDIA_VISIBLE_DEVICES do not), so it
    is the one thing that can clear a container that is exposed to every card.

    `raw` is the container's `docker inspect` object; only its GPU wiring is read and no value
    from it is quoted back. `gpu_indexes` maps nvidia-smi indexes to uuids (the host inventory);
    an index it cannot resolve proves nothing. Without the leased card's uuid nothing can be
    proven, so every GPU container is refused."""
    if not _requests_gpu(raw):
        return None
    reason = ("it may use the leased GPU (the Ordo compute card the scheduler arbitrates); restarting it "
              "could put a second tenant on that card. Pin it to another card with CUDA_VISIBLE_DEVICES, "
              "or restart it on the host.")
    if not leased_uuid:
        return "the leased GPU's uuid is unknown, so a GPU container cannot be proven off it: " + reason
    leased = leased_uuid.lower()
    cuda = _env_devices(raw, "CUDA_VISIBLE_DEVICES", gpu_indexes)
    if cuda and leased not in cuda:
        return None                                # pinned to other cards only
    exposed = _exposed_devices(raw, gpu_indexes)
    if exposed is _ANY_CARD or leased in exposed:
        return reason
    return None


class RestartBudget:
    """At most `limit` restarts per key (`<project>/<container>`) in any sliding `window_seconds`.
    Only restarts that were carried out are recorded: a refused call spends nothing."""

    def __init__(self, limit: int = RESTARTS_PER_WINDOW, window_seconds: float = RESTART_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._restarts: dict[str, deque[float]] = {}

    def _recent(self, key: str, now: float) -> deque[float]:
        times = self._restarts.setdefault(key, deque())
        while times and now - times[0] >= self.window_seconds:
            times.popleft()
        return times

    def retry_after(self, key: str) -> int | None:
        """Seconds until one more restart of `key` is allowed, or None when it is allowed now."""
        with self._lock:
            now = self._clock()
            times = self._recent(key, now)
            if len(times) < self.limit:
                return None
            return max(1, math.ceil(self.window_seconds - (now - times[0])))

    def record(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            self._recent(key, now).append(now)
