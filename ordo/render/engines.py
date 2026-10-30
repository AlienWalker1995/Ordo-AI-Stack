"""What a model-serving service serves, stamped on its rendered compose service as labels.

A service that serves a model declares it: a plugin service with `serves:` in its manifest, and
the core chat engine through the render. The render writes the declaration as `ordo.serves.*`
labels, so the rendered compose and every running container say which engines exist, which device
each runs on and how its metrics are read. Nothing downstream keeps a list of engine service names:
a third-party plugin that declares `serves:` shows up in the registry, the metrics and the
dashboard with no code change.

`serves:` in a plugin manifest:

    serves:
      model: local-chat-cpu      # the id clients address it by (a model-gateway alias for chat)
      kind: chat                 # chat | embedding | image | stt | tts
      metrics: llamacpp          # how its throughput is read: llamacpp | ninfer-log | none
      metrics_path: /metrics     # where an HTTP metrics format is served (default /metrics)
      fallback_for: local-chat   # optional: the model this one stands in for while it is down
      model_env: WHISPER__MODEL  # optional: the service env key naming its model (not a file)

The device is not declared: it is read from how the render wired the service (a GPU reservation
or device nodes -> gpu, otherwise cpu), so it cannot disagree with what the service gets.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

KINDS = ("chat", "embedding", "image", "stt", "tts")
# `ninfer-log` is a temporary reader: NInfer has no metrics endpoint yet, so its throughput is read
# from its request log until it serves one upstream.
METRICS_FORMATS = ("llamacpp", "ninfer-log", "none")

LABEL_MODEL = "ordo.serves.model"
LABEL_KIND = "ordo.serves.kind"
LABEL_DEVICE = "ordo.serves.device"
LABEL_METRICS = "ordo.serves.metrics"
LABEL_METRICS_PATH = "ordo.serves.metrics-path"
LABEL_FALLBACK_FOR = "ordo.serves.fallback-for"
LABEL_MODEL_ENV = "ordo.serves.model-env"


@dataclasses.dataclass(frozen=True)
class Serves:
    model: str
    kind: str
    metrics: str = "none"
    metrics_path: str = "/metrics"
    fallback_for: str = ""
    model_env: str = ""

    @classmethod
    def from_manifest(cls, raw: Any, where: str) -> Serves | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ValueError(f"{where}: `serves` must be a mapping")
        unknown = set(raw) - {"model", "kind", "metrics", "metrics_path", "fallback_for", "model_env"}
        if unknown:
            raise ValueError(f"{where}: `serves` has unknown keys {sorted(unknown)}")
        model = str(raw.get("model") or "")
        if not model:
            raise ValueError(f"{where}: `serves.model` is required")
        kind = str(raw.get("kind") or "")
        if kind not in KINDS:
            raise ValueError(f"{where}: `serves.kind` must be one of {list(KINDS)}, not {kind!r}")
        metrics = str(raw.get("metrics") or "none")
        if metrics not in METRICS_FORMATS:
            raise ValueError(f"{where}: `serves.metrics` must be one of {list(METRICS_FORMATS)}, not {metrics!r}")
        metrics_path = str(raw.get("metrics_path") or "/metrics")
        if not metrics_path.startswith("/"):
            raise ValueError(f"{where}: `serves.metrics_path` must start with '/'")
        return cls(model=model, kind=kind, metrics=metrics, metrics_path=metrics_path,
                   fallback_for=str(raw.get("fallback_for") or ""),
                   model_env=str(raw.get("model_env") or ""))


def device_of(svc: Mapping[str, Any]) -> str:
    """"gpu" when the rendered service reserves a GPU or is handed GPU device nodes, else "cpu"."""
    reservations = ((svc.get("deploy") or {}).get("resources") or {}).get("reservations") or {}
    if reservations.get("devices") or svc.get("devices"):
        return "gpu"
    return "cpu"


def stamp(svc: dict[str, Any], serves: Serves) -> None:
    """Write `serves` and the service's device as labels. Call after the GPU wiring is final."""
    labels = svc.setdefault("labels", {})
    labels[LABEL_MODEL] = serves.model
    labels[LABEL_KIND] = serves.kind
    labels[LABEL_DEVICE] = device_of(svc)
    labels[LABEL_METRICS] = serves.metrics
    labels[LABEL_METRICS_PATH] = serves.metrics_path
    if serves.fallback_for:
        labels[LABEL_FALLBACK_FOR] = serves.fallback_for
    if serves.model_env:
        labels[LABEL_MODEL_ENV] = serves.model_env


def read(labels: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The engine declaration in a service's (or container's) labels, or None when it serves no model."""
    labels = labels or {}
    model = labels.get(LABEL_MODEL)
    if not model:
        return None
    return {
        "model": str(model),
        "kind": str(labels.get(LABEL_KIND, "")),
        "device": str(labels.get(LABEL_DEVICE, "cpu")),
        "metrics": str(labels.get(LABEL_METRICS, "none")),
        "metrics_path": str(labels.get(LABEL_METRICS_PATH, "/metrics")),
        "fallback_for": str(labels.get(LABEL_FALLBACK_FOR, "")),
        "model_env": str(labels.get(LABEL_MODEL_ENV, "")),
    }
