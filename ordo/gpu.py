"""Declarative GPU arbitration — who competes for which card, how, and under what contract.

The arbiter already exists: `ordo serve`'s Scheduler + Broker is a global queue for GPU
RESIDENCY (`POST /jobs` = "grant me VRAM", `POST /jobs/complete` = "I'm done"). What was missing
is the other half — a way for a service to DECLARE that it competes for the card. Without that,
the arbiter learned the shape of contention from tribal knowledge: `ordo serve` carried a
`--resident-service llamacpp` default, ops-api carried a `COMFYUI_GUARDIAN_TARGET` env, and
every other GPU service was arbitrated only by whoever remembered to call `/jobs`. That gap is
what let a hand-queued ComfyUI render share the 5090 with the resident llama.cpp for four hours
at ~98% VRAM on 2026-08-08 until the graphics kernel bugchecked (0x113).

This module makes GPU contention a DECLARED, testable property of a compose service:

    services:
      - name: comfyui
        gpu_pin: primary
        gpu_arbitration:
          mode: burst
          enforcement: gate
          vram_gb: exclusive
          gate: {upstream_port: 8188, submit_paths: [/prompt], queue_path: /queue}

Two independent axes, deliberately not conflated:

`mode` — HOW the service holds VRAM.
  - ``resident``  Holds VRAM continuously for as long as it runs and serves many short requests
                  from it (llama.cpp: chat completions; llamacpp-embed: embeddings). A resident
                  enters the global queue ONCE, for residency — its individual inference calls
                  never do. That distinction is the whole point: if a chat completion had to
                  queue behind a four-minute render as a peer, interactive inference would be
                  unusable. The queue arbitrates HANDOVERS OF THE CARD, not GPU operations.
  - ``burst``     Idle most of the time; needs the card only while a unit of work runs, and its
                  demand appears dynamically (ComfyUI when a prompt is queued, ltx-trainer when
                  a run starts). Enters the queue per unit of work and leaves on completion.
  - ``exempt``    Touches the GPU but holds no meaningful VRAM — a read-only ``utility``
                  capability (nvidia-smi/NVML for detection or metrics), no compute context.
                  Declared explicitly so "no arbitration needed" is a decision on the record
                  rather than an omission nobody noticed.

`enforcement` — HOW the service is made to enter the queue. A declaration that nothing enforces
is a comment, and comments did not stop the 2026-08-08 incident.
  - ``broker``    The arbiter itself holds the entry: `ordo serve` registers the service's
                  residency and the Broker starts/stops the container to grant or reclaim it.
                  The service does nothing. Only valid for ``resident``.
  - ``client``    The service (or its launcher) calls `/jobs` itself — the
                  ``assets/lease-exec.py`` contract, used by ltx-trainer.
  - ``gate``      A companion admission gate is rendered in front of the service's submission
                  API and enters the queue on its behalf, BEFORE the work is allowed to start.
                  This is the only enforcement that works for a long-running server whose work
                  can be submitted by a human in a web UI that has never heard of the scheduler.
                  See ``services/gpu-gate``.
  - ``none``      Only valid for ``exempt``.

`vram_gb` is the RUNTIME footprint on the card — a different question from the plugin-level
``requires.vram_gb`` (the minimum card the service can run on at all, an admission/fit gate).
``vram_gb: exclusive`` resolves at render time to the whole card, which is how a service says
"this work must not share the GPU": the arbiter must reclaim every resident to admit it and can
co-run nothing beside it. A sentinel rather than a magic number keeps that correct on a 16GB box
and a 32GB box alike.

`device` says WHICH GPU the service contends for. This box runs two (a 5090 for compute, a
Pascal 1070 for the small voice models). The arbiter's VRAM accounting covers the primary card,
so a ``device: secondary`` service is deliberately OUTSIDE that accounting rather than
accidentally missing from it — Whisper and Kokoro must not contend with renders at all.

`preemptible` + `yield` are the reclamation contract for a resident, and they are two different
questions. `preemptible` says the arbiter MAY reclaim the card; `yield` says WHAT HAPPENS TO THE
CAPABILITY when it does. Not all preemptible services are alike:

  - A burst render has no degraded path — reclaiming it means the work stops. ``strategy: stop``.
  - The resident LLM does: it MIGRATES rather than dies. ``llamacpp-cpu`` runs its own CPU
    chat model at the same context window, and the model-gateway (LiteLLM) already carries
    ``fallbacks: [{local-chat: [<cpu pin alias>]}]`` with a 30s cooldown that routes back to
    the GPU model as soon as it is healthy again. So llama.cpp yielding the 5090 is a
    GPU→CPU migration: availability is preserved, only throughput degrades. That is declared as
    ``strategy: failover`` with the degraded target and the component that reroutes named as
    data, so the relationship is inspectable instead of being folklore split across a LiteLLM
    config comment and a plugin README.

What ``failover`` honestly describes today is ERROR-DRIVEN failover: the Broker stops the GPU
container, LiteLLM discovers the deployment unhealthy and reroutes. Requests that were streaming
at that instant die. The clean contract — quiesce, route new work to the degraded target, drain
in-flight completions under a bounded timeout, only THEN release VRAM, then ack the yield — is
``strategy: handover``, and it is deliberately NOT accepted yet: ``SUPPORTED_YIELD_STRATEGIES``
refuses to parse a strategy the runtime cannot actually honour, so a manifest can never claim a
guarantee the stack does not provide. Ordering in that contract is load-bearing (route away
before draining, drain before releasing, never release while a completion is streaming), which
is precisely why it is its own change rather than a flag added here.

Nothing in this module starts or stops anything, and nothing here is a second arbiter. It only
DESCRIBES the contention; the Scheduler decides and the Broker acts. There is exactly one GPU
arbiter — ``ordo serve``.
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hardware import HardwareProfile

MODES = ("resident", "burst", "exempt")
ENFORCEMENTS = ("broker", "client", "gate", "none")
DEVICES = ("primary", "secondary")

# Which `mode` may use which `enforcement`. A `resident` cannot be enforced by a gate (there is
# no per-unit submission to intercept — it holds the card continuously), and a `burst` service
# cannot be broker-enforced (the broker has no idea when its work starts).
VALID_ENFORCEMENT: dict[str, tuple[str, ...]] = {
    "resident": ("broker",),
    "burst": ("client", "gate"),
    "exempt": ("none",),
}

# Yield strategies the RUNTIME can actually honour today:
#   stop      the Broker stops the container; the capability goes away until residency returns.
#   failover  same reclamation, but a declared degraded service keeps the capability available
#             (llama.cpp -> llamacpp-cpu via the model-gateway's LiteLLM fallback). Availability
#             survives; in-flight work at the moment of the stop does not.
# `handover` — quiesce, reroute, drain in-flight under a bounded timeout, release, ack — is the
# next stage of this work and is deliberately NOT accepted: parsing a strategy nothing implements
# would let a manifest claim a guarantee the stack does not provide.
SUPPORTED_YIELD_STRATEGIES = ("stop", "failover")
YIELD_STRATEGIES = ("stop", "failover", "handover")

# `vram_gb: exclusive` — the whole card, resolved against real hardware at render time.
EXCLUSIVE = "exclusive"

# Suffix for the companion admission gate rendered in front of a `gate`-enforced service.
GATE_SUFFIX = "-gate"


class GpuArbitrationError(ValueError):
    """A malformed `gpu_arbitration:` block. Raised at load time so a bad manifest fails the
    render (and CI) rather than silently rendering an unarbitrated GPU service."""


@dataclasses.dataclass(frozen=True)
class YieldSpec:
    """What happens to a service's CAPABILITY when the arbiter reclaims its VRAM.

    `degraded_service` / `degraded_via` name the migration target and the component that
    reroutes to it — for llama.cpp, `llamacpp-cpu` and `model-gateway`. Naming them here makes
    the relationship a checkable property (tests/substrate asserts the target exists and serves
    the same context window) instead of folklore spread across a LiteLLM config comment and a
    plugin README.
    """
    strategy: str = "stop"
    degraded_service: str = ""
    degraded_via: str = ""
    # Bounded wait for in-flight work before VRAM is released. Consumed by the `handover`
    # strategy; carried now so the contract is declared in one place when it lands.
    drain_timeout_seconds: float = 120.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, *, where: str = "") -> YieldSpec:
        d = d or {}
        strategy = str(d.get("strategy", "stop") or "stop")
        if strategy not in YIELD_STRATEGIES:
            raise GpuArbitrationError(
                f"{where}gpu_arbitration.yield.strategy must be one of "
                f"{list(YIELD_STRATEGIES)}, got {strategy!r}")
        if strategy not in SUPPORTED_YIELD_STRATEGIES:
            raise GpuArbitrationError(
                f"{where}gpu_arbitration.yield.strategy={strategy!r} is not implemented — "
                f"supported: {list(SUPPORTED_YIELD_STRATEGIES)}. The clean handover (quiesce, "
                f"reroute new work to the degraded service, drain in-flight completions, only "
                f"then release VRAM, then ack) is a separate change; declaring it before the "
                f"runtime honours it would promise a guarantee the stack does not provide.")
        spec = cls(
            strategy=strategy,
            degraded_service=str(d.get("degraded_service", "") or ""),
            degraded_via=str(d.get("degraded_via", "") or ""),
            drain_timeout_seconds=float(d.get("drain_timeout_seconds", 120.0)),
        )
        if strategy == "failover" and not (spec.degraded_service and spec.degraded_via):
            raise GpuArbitrationError(
                f"{where}gpu_arbitration.yield.strategy 'failover' requires both "
                f"degraded_service and degraded_via — a failover with no named target is a stop")
        return spec


@dataclasses.dataclass(frozen=True)
class GateSpec:
    """How a gate-enforced service's submission API is intercepted.

    Declared per service because "which request starts GPU work" is service knowledge, not
    arbiter knowledge — the gate image itself stays generic. `queue_path` is what the gate polls
    to learn when the work has actually drained: a submit call returns as soon as the job is
    accepted, so the HTTP response is NOT the release signal.
    """
    upstream_port: int = 0
    submit_paths: tuple[str, ...] = ()
    submit_methods: tuple[str, ...] = ("POST",)
    queue_path: str = ""
    queue_style: str = "comfyui"   # how to read `queue_path`'s JSON; see services/gpu-gate
    drain_seconds: float = 60.0    # queue must stay empty this long before residency is released
    acquire_timeout_seconds: float = 120.0  # max a human waits at the UI before an honest refusal
    listen_port: int = 0           # gate's own port; defaults to upstream_port (drop-in swap)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> GateSpec:
        d = d or {}
        port = int(d.get("upstream_port", 0) or 0)
        return cls(
            upstream_port=port,
            submit_paths=tuple(str(p) for p in (d.get("submit_paths", []) or [])),
            submit_methods=tuple(str(m).upper() for m in (d.get("submit_methods", []) or ["POST"])),
            queue_path=str(d.get("queue_path", "") or ""),
            queue_style=str(d.get("queue_style", "") or "comfyui"),
            drain_seconds=float(d.get("drain_seconds", 60.0)),
            acquire_timeout_seconds=float(d.get("acquire_timeout_seconds", 120.0)),
            listen_port=int(d.get("listen_port", 0) or 0) or port,
        )


@dataclasses.dataclass(frozen=True)
class GpuArbitration:
    """How ONE compose service's use of a GPU is arbitrated. Data — no behaviour."""
    mode: str
    enforcement: str
    vram_gb: float | str = 0.0     # runtime footprint, or the EXCLUSIVE sentinel
    device: str = "primary"
    est_seconds: float = 0.0       # duration hint for the arbiter's busy-ETA + lease TTL
    kind: str = ""                 # job `kind` label; defaults from the mode
    preemptible: bool = True       # may the arbiter reclaim this resident's VRAM?
    yields: YieldSpec = dataclasses.field(default_factory=YieldSpec)  # `yield:` in YAML
    gate: GateSpec = dataclasses.field(default_factory=GateSpec)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, *, service: str = "",
                  default_device: str = "primary") -> GpuArbitration | None:
        """Parse a `gpu_arbitration:` block. Absent -> None (the caller decides whether that is
        allowed; for a service that reserves a GPU it is NOT — see tests/substrate)."""
        if not d:
            return None
        where = f"service '{service}': " if service else ""
        mode = str(d.get("mode", "")).strip()
        if mode not in MODES:
            raise GpuArbitrationError(
                f"{where}gpu_arbitration.mode must be one of {list(MODES)}, got {mode!r}")
        enforcement = str(d.get("enforcement", "")).strip()
        if enforcement not in ENFORCEMENTS:
            raise GpuArbitrationError(
                f"{where}gpu_arbitration.enforcement must be one of {list(ENFORCEMENTS)}, "
                f"got {enforcement!r}")
        if enforcement not in VALID_ENFORCEMENT[mode]:
            raise GpuArbitrationError(
                f"{where}mode '{mode}' cannot be enforced by '{enforcement}' — valid: "
                f"{list(VALID_ENFORCEMENT[mode])}")
        device = str(d.get("device", "") or default_device)
        if device not in DEVICES:
            raise GpuArbitrationError(
                f"{where}gpu_arbitration.device must be one of {list(DEVICES)}, got {device!r}")
        raw_vram = d.get("vram_gb", 0.0)
        vram: float | str
        if isinstance(raw_vram, str) and raw_vram.strip().lower() == EXCLUSIVE:
            vram = EXCLUSIVE
        else:
            try:
                vram = float(raw_vram)
            except (TypeError, ValueError) as e:
                raise GpuArbitrationError(
                    f"{where}gpu_arbitration.vram_gb must be a number or '{EXCLUSIVE}'") from e
        arb = cls(
            mode=mode, enforcement=enforcement, vram_gb=vram, device=device,
            est_seconds=float(d.get("est_seconds", 0.0)),
            kind=str(d.get("kind", "") or ""),
            preemptible=bool(d.get("preemptible", True)),
            # `yield` is a Python keyword, so the field is `yields`; the YAML key stays `yield`.
            yields=YieldSpec.from_dict(d.get("yield"), where=where),
            gate=GateSpec.from_dict(d.get("gate")),
        )
        arb._validate(where)
        return arb

    def _validate(self, where: str) -> None:
        if self.mode != "exempt" and self.vram_gb in (0.0, 0):
            raise GpuArbitrationError(
                f"{where}gpu_arbitration.vram_gb is required for mode '{self.mode}' — the "
                f"arbiter cannot grant residency to a service with no declared footprint")
        if self.enforcement == "gate":
            if not self.gate.submit_paths:
                raise GpuArbitrationError(
                    f"{where}gpu_arbitration.gate.submit_paths is required for enforcement "
                    f"'gate' — the gate must know which request starts GPU work")
            if not self.gate.queue_path:
                raise GpuArbitrationError(
                    f"{where}gpu_arbitration.gate.queue_path is required for enforcement 'gate' "
                    f"— the gate must know when the work has drained (a submit returns early)")
            if not self.gate.upstream_port:
                raise GpuArbitrationError(
                    f"{where}gpu_arbitration.gate.upstream_port is required for enforcement "
                    f"'gate'")
        if self.mode != "resident" and self.yields.strategy == "failover":
            raise GpuArbitrationError(
                f"{where}gpu_arbitration.yield.strategy 'failover' only means something for a "
                f"resident — a burst service that is not running has nothing to fail over from")

    @property
    def job_kind(self) -> str:
        """The `kind` label the residency request carries. Explicit `kind:` wins."""
        return self.kind or ("media" if self.enforcement == "gate" else self.mode)

    def resolve_vram_gb(self, hw: HardwareProfile) -> float:
        """The concrete footprint in GB. `exclusive` becomes the whole declared device, so an
        exclusive request forces reclamation of every resident and admits no co-runner — on any
        size of card. A device that isn't present resolves to 0.0 (the service is gated off)."""
        if self.vram_gb == EXCLUSIVE:
            return round(self._device_total_gb(hw), 2)
        return float(self.vram_gb)

    def _device_total_gb(self, hw: HardwareProfile) -> float:
        if self.device == "secondary":
            sec = hw.secondary_gpu
            return float(sec.vram_gb) if sec else 0.0
        return float(hw.primary_vram_gb) if hw.has_gpu else 0.0


@dataclasses.dataclass(frozen=True)
class GpuClaim:
    """One service's RESOLVED claim on a GPU — the inventory row the arbiter derives from."""
    service: str
    owner: str          # plugin id, or "core" for a substrate service
    mode: str
    enforcement: str
    device: str
    vram_gb: float      # resolved against real hardware (exclusive -> full device)
    est_seconds: float
    kind: str
    preemptible: bool
    yield_strategy: str
    degraded_service: str

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --- core (substrate) services -------------------------------------------------------------
# The core services have no plugin.yaml — their images, env and GPU reservations are rendered by
# ordo/compose.py, so their arbitration is declared here, next to them. Same one-source-of-truth
# rule the manifests follow, applied to the services that ARE the substrate. A core service that
# renders a GPU reservation and is missing from this table fails
# tests/substrate/test_gpu_arbitration.py.
#
# llamacpp's footprint is deliberately 0.0 in the table: the real number is weights + KV at the
# RENDERED context, which only the render knows (RenderedConfig.resident_vram_gb). `inventory()`
# fills it in, so it can never drift from what `.env` actually tells llama-server to load.
CORE_GPU_ARBITRATION: dict[str, GpuArbitration] = {
    # The resident LLM. Enters the global queue once, for residency; the completions it serves
    # from that residency never queue individually. Preemptible: an exclusive burst request
    # reclaims it and the Broker restores it when the work drains.
    #
    # It does NOT die when it yields — it migrates to CPU. `llamacpp-cpu` runs the same Qwen3.6
    # A3B at the same 131072 window and the model-gateway's LiteLLM config already fails
    # `local-chat` over to it, then routes back on a 30s cooldown once the GPU model is healthy.
    # `failover` records exactly that, and no more: today the reroute is error-driven (the Broker
    # stops the container, LiteLLM discovers it unhealthy), so completions streaming at that
    # instant are lost. The clean `handover` is the next change — see SUPPORTED_YIELD_STRATEGIES.
    "llamacpp": GpuArbitration(mode="resident", enforcement="broker", vram_gb=0.0,
                               device="primary", preemptible=True,
                               yields=YieldSpec(strategy="failover",
                                                degraded_service="llamacpp-cpu",
                                                degraded_via="model-gateway")),
    # Read-only `utility` capability only (nvidia-smi/NVML for VRAM detection and the dashboard's
    # GPU widgets) — no compute context, no meaningful VRAM. See compose._utility_gpu_reservation.
    "ops-controller": GpuArbitration(mode="exempt", enforcement="none", device="primary"),
    "dashboard": GpuArbitration(mode="exempt", enforcement="none", device="primary"),
    "ops-api": GpuArbitration(mode="exempt", enforcement="none", device="primary"),
}


def gate_service_name(service: str) -> str:
    """Compose service name of the admission gate rendered in front of a gated service."""
    return f"{service}{GATE_SUFFIX}"


def inventory(
    hw: HardwareProfile,
    plugin_services: list[Any],
    *,
    resident_vram_gb: float = 0.0,
    core: dict[str, GpuArbitration] | None = None,
) -> list[GpuClaim]:
    """Every declared GPU claim on this box: core services + the enabled plugins' services.

    This is what replaces `--resident-service llamacpp` and `COMFYUI_GUARDIAN_TARGET`: the
    arbiter no longer knows any service by name, it reads this.
    """
    core = CORE_GPU_ARBITRATION if core is None else core
    claims: list[GpuClaim] = []

    def add(service: str, owner: str, arb: GpuArbitration, vram: float) -> None:
        claims.append(GpuClaim(
            service=service, owner=owner, mode=arb.mode, enforcement=arb.enforcement,
            device=arb.device, vram_gb=round(float(vram), 2), est_seconds=arb.est_seconds,
            kind=arb.job_kind, preemptible=arb.preemptible,
            yield_strategy=arb.yields.strategy, degraded_service=arb.yields.degraded_service))

    for name, arb in core.items():
        # The one dynamic footprint: the resident LLM's weights+KV come from the render.
        vram = resident_vram_gb if (arb.mode == "resident" and not arb.vram_gb) \
            else arb.resolve_vram_gb(hw)
        add(name, "core", arb, vram)
    for plugin, ps in plugin_services:
        if ps.gpu_arbitration is not None:
            add(ps.name, plugin.id, ps.gpu_arbitration, ps.gpu_arbitration.resolve_vram_gb(hw))
    return claims


def primary_residents(claims: list[GpuClaim]) -> dict[str, float]:
    """service -> footprint for every PREEMPTIBLE resident on the primary device.

    `ordo serve` registers exactly these with the Scheduler as idle-cached (reclaimable) models.
    Two exclusions are deliberate:
      * secondary-device residents — the Scheduler models ONE card's VRAM, and folding the 1070's
        residents into the 5090's budget would mis-admit every job;
      * non-preemptible residents — see `pinned_primary_vram_gb`, which removes their VRAM from
        the budget entirely instead, so nothing can ever be admitted into it.
    """
    return {c.service: c.vram_gb for c in claims
            if c.mode == "resident" and c.device == "primary" and c.preemptible and c.vram_gb > 0}


def pinned_primary_vram_gb(claims: list[GpuClaim]) -> float:
    """VRAM held on the primary device by residents the arbiter may NOT reclaim.

    Subtracted from the Scheduler's total so that capacity is invisible to admission: a
    non-preemptible resident's card space is simply not on offer, rather than being offered and
    then defended. Zero on this stack today (llama.cpp is preemptible) — it exists so an operator
    can declare "never evict the LLM" as data instead of as a code change.
    """
    return round(sum(c.vram_gb for c in claims
                     if c.mode == "resident" and c.device == "primary" and not c.preemptible), 2)


def gated_claims(claims: list[GpuClaim]) -> list[GpuClaim]:
    """Services whose GPU use is enforced by a companion admission gate."""
    return [c for c in claims if c.enforcement == "gate"]
