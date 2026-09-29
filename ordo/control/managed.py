"""Maintenance of OTHER compose projects on this host: status, logs and a confirmed restart.

The operator lists the projects Hermes may maintain as `managed_projects:` in ordo.yaml (the
source of truth; `Source.validate` refuses Ordo's own project). ops-controller serves them under
`/projects/...` (ordo/control/api.py). This module holds the pure policy those routes apply:

- `gpu_refusal`: a restart must never put a second tenant on the GPU the scheduler leases. Ordo's
  lease-managed residents are out of reach by construction (Ordo's project cannot be listed); this
  guard covers a FOREIGN container whose device requests or NVIDIA_VISIBLE_DEVICES expose the
  leased card, unless CUDA_VISIBLE_DEVICES pins it elsewhere (the one rule is in that function).
- `RestartBudget`: at most `limit` restarts per container per sliding window (3 per hour), so a
  confused agent cannot flap a service; the next one is refused with 429 and a retry time. The
  budget is held in ops-controller's memory: restarting ops-controller resets it.

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

# A full-length NVIDIA GPU uuid. CUDA accepts any unambiguous PREFIX of one, so a shorter token
# can name the leased card: only a full uuid that matches a card in the host inventory counts.
_FULL_GPU_UUID = re.compile(r"^gpu-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Host device nodes that hand a container a GPU without any device request: the NVIDIA device
# files, and /dev/dxg (the WSL2 GPU paravirtualization node, which exposes every card).
_GPU_DEVICE_NODES = ("/dev/nvidia", "/dev/dxg", "/dev/dri")
# A CDI device name for an NVIDIA GPU: `nvidia.com/gpu=<all | index | uuid>`.
_CDI_GPU_PREFIX = "nvidia.com/gpu"


def _env_value(raw: dict, key: str) -> str | None:
    for entry in (raw.get("Config") or {}).get("Env") or []:
        name, sep, value = str(entry).partition("=")
        if sep and name == key:
            return value.strip()
    return None


def _gpu_device_nodes(raw: dict) -> bool:
    for device in (raw.get("HostConfig") or {}).get("Devices") or []:
        if str(device.get("PathOnHost") or "").startswith(_GPU_DEVICE_NODES):
            return True
    return False


def _requests_gpu(raw: dict) -> bool:
    """Whether the container is given any GPU. Every device request counts, whatever its driver
    (nvidia, cdi, empty or unknown): docker's device requests exist for GPUs, and an unknown
    driver cannot be proven not to be one. So do the nvidia runtime and GPU device nodes."""
    host_config = raw.get("HostConfig") or {}
    if str(host_config.get("Runtime") or "") == "nvidia":
        return True
    if host_config.get("DeviceRequests"):
        return True
    return _gpu_device_nodes(raw)


# A device list that names every card, or a card nobody can identify.
_ANY_CARD = None


def _resolve(devices: list[str], gpu_indexes: Mapping[str, str], *, indexes_are_pci_order: bool) -> set[str] | None:
    """Device tokens as the lower-case uuids of cards on this host, or None when any token cannot
    be matched to exactly one of them.

    - A uuid counts only at full length AND only when the host inventory has that card: CUDA
      accepts uuid prefixes, and a token no card matches proves nothing.
    - An index counts only when the caller says it is in nvidia-smi (PCI bus) order, the order
      `gpu_indexes` uses. CUDA's own default order is fastest first, so a CUDA index is PCI order
      only with CUDA_DEVICE_ORDER=PCI_BUS_ID.
    - Anything else (`all`, a MIG id, an unknown index) is unresolved."""
    known = {uuid.lower() for uuid in gpu_indexes.values()}
    uuids: set[str] = set()
    for token in (d.strip() for d in devices):
        if not token:
            continue
        lowered = token.lower()
        if _FULL_GPU_UUID.match(lowered) and lowered in known:
            uuids.add(lowered)
        elif indexes_are_pci_order and token in gpu_indexes:
            uuids.add(gpu_indexes[token].lower())
        else:
            return _ANY_CARD
    return uuids


def _env_devices(raw: dict, key: str, gpu_indexes: Mapping[str, str], *,
                 indexes_are_pci_order: bool) -> set[str] | None:
    """The cards an env pin names: None for `all` or unset (the caller decides what unset means),
    empty for `none`/`void`."""
    value = _env_value(raw, key)
    if value is None or value.lower() == "all":
        return _ANY_CARD
    if value.lower() in ("none", "void", ""):
        return set()
    return _resolve(value.split(","), gpu_indexes, indexes_are_pci_order=indexes_are_pci_order)


def _request_devices(request: dict, gpu_indexes: Mapping[str, str]) -> set[str] | None:
    """The cards one device request names. A count (or -1) lets docker pick: any card. An
    nvidia request's ids are NVML indexes (PCI order) or uuids. A CDI id is `nvidia.com/gpu=X`;
    only a full uuid there is trusted. Any other driver, or an id that is not a GPU name, is
    unresolved."""
    device_ids = [str(d) for d in request.get("DeviceIDs") or []]
    if not device_ids:
        return _ANY_CARD
    driver = str(request.get("Driver") or "")
    if driver == "nvidia":
        return _resolve(device_ids, gpu_indexes, indexes_are_pci_order=True)
    names = []
    for device_id in device_ids:
        kind, sep, name = device_id.partition("=")
        if not sep or kind != _CDI_GPU_PREFIX:
            return _ANY_CARD
        names.append(name)
    return _resolve(names, gpu_indexes, indexes_are_pci_order=False)


def _exposed_devices(raw: dict, gpu_indexes: Mapping[str, str]) -> set[str] | None:
    """Every card the container is given, from its device requests, GPU device nodes AND
    NVIDIA_VISIBLE_DEVICES (the union: any one can hand it a card). None means any card."""
    host_config = raw.get("HostConfig") or {}
    if _gpu_device_nodes(raw):
        return _ANY_CARD
    exposed: set[str] = set()
    requests = host_config.get("DeviceRequests") or []
    for request in requests:
        devices = _request_devices(request, gpu_indexes)
        if devices is _ANY_CARD:
            return _ANY_CARD
        exposed |= devices
    if _env_value(raw, "NVIDIA_VISIBLE_DEVICES") is not None or not requests:
        # Set, it can widen what the requests give; unset with only the nvidia runtime, the
        # runtime's default is every card. Its indexes are NVML's (PCI order).
        from_env = _env_devices(raw, "NVIDIA_VISIBLE_DEVICES", gpu_indexes, indexes_are_pci_order=True)
        if from_env is _ANY_CARD:
            return _ANY_CARD
        exposed |= from_env
    return exposed


def gpu_refusal(raw: dict, leased_uuid: str | None, gpu_indexes: Mapping[str, str]) -> str | None:
    """Why restarting this container could touch the leased GPU, or None when it cannot.

    The one rule: refuse when the container's device requests (any driver, CDI included), GPU
    device nodes or NVIDIA_VISIBLE_DEVICES expose the leased card (by uuid, by index, as `all`,
    or as a bare count that lets docker pick), UNLESS CUDA_VISIBLE_DEVICES pins it to other
    cards only. On Docker Desktop/WSL2 CUDA_VISIBLE_DEVICES is the only pin that isolates a
    process (device ids and NVIDIA_VISIBLE_DEVICES do not), so it is the one thing that can clear
    a container that is exposed to every card. It clears only when every entry is a full uuid of
    another card in the host inventory, or an index with CUDA_DEVICE_ORDER=PCI_BUS_ID.

    `raw` is the container's `docker inspect` object; only its GPU wiring is read and no value
    from it is quoted back. `gpu_indexes` maps nvidia-smi indexes to uuids (the host inventory);
    without it nothing resolves. Without the leased card's uuid nothing can be proven, so every
    GPU container is refused."""
    if not _requests_gpu(raw):
        return None
    reason = ("it may use the leased GPU (the Ordo compute card the scheduler arbitrates); restarting it "
              "could put a second tenant on that card. Pin it to another card with CUDA_VISIBLE_DEVICES "
              "(a full GPU uuid), or restart it on the host.")
    if not leased_uuid:
        return "the leased GPU's uuid is unknown, so a GPU container cannot be proven off it: " + reason
    leased = leased_uuid.lower()
    pci_order = (_env_value(raw, "CUDA_DEVICE_ORDER") or "") == "PCI_BUS_ID"
    cuda = _env_devices(raw, "CUDA_VISIBLE_DEVICES", gpu_indexes, indexes_are_pci_order=pci_order)
    if cuda and leased not in cuda:
        return None                                # pinned to other cards only
    exposed = _exposed_devices(raw, gpu_indexes)
    if exposed is _ANY_CARD or leased in exposed:
        return reason
    return None


class RestartBudget:
    """At most `limit` restarts per key (`<project>/<container>`) in any sliding `window_seconds`.

    `reserve` checks and takes a slot under one lock, so concurrent callers can never get more
    than `limit` between them; a caller whose restart then fails gives the slot back with
    `refund`. The budget lives in ops-controller's memory: an ops-controller restart resets it."""

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

    def reserve(self, key: str) -> int | None:
        """Take one restart slot for `key` and return None, or return the seconds until one frees
        up (nothing taken)."""
        with self._lock:
            now = self._clock()
            times = self._recent(key, now)
            if len(times) >= self.limit:
                return max(1, math.ceil(self.window_seconds - (now - times[0])))
            times.append(now)
            return None

    def refund(self, key: str) -> None:
        """Give back the newest slot of `key`: its restart did not happen."""
        with self._lock:
            times = self._restarts.get(key)
            if times:
                times.pop()
