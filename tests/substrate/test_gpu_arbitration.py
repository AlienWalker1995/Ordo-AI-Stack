"""GPU arbitration is DECLARED, derived, and enforced — not tribal knowledge.

The 2026-08-08 host bugcheck (0x113) happened because nothing in the config said "ComfyUI
competes with llama.cpp for the 5090". The arbiter learned contention from a
`--resident-service llamacpp` CLI default and an ops-api `COMFYUI_GUARDIAN_TARGET` env, and a
render queued by hand in the web UI passed through neither.

These tests lock the replacement in place:
  * every service that renders a GPU reservation DECLARES how it is arbitrated (so a new GPU
    service cannot ship unarbitrated — that omission is the whole bug class);
  * the arbiter DERIVES its resident set from those declarations rather than from a name;
  * a `gate`-enforced service always renders its gate, and its in-stack consumers are pointed
    at the gate rather than around it;
  * the schema refuses to describe guarantees the runtime does not provide.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ordo import compose, gpu
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.hardware import GPU, HardwareProfile
from ordo.plugins import PluginRegistry
from ordo.render import DEFAULT_PLUGINS_DIR, GATED_SERVICE_URL_ENV, render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")

DUAL_GPU = {
    "gpus": [{"name": "RTX 5090", "vram_gb": 32.0, "uuid": "GPU-primary"},
             {"name": "GTX 1070", "vram_gb": 8.0, "uuid": "GPU-secondary"}],
    "ram_gb": 128.0, "cpu_cores": 32, "platform": "Linux",
}


@pytest.fixture
def registry() -> PluginRegistry:
    return PluginRegistry.load(DEFAULT_PLUGINS_DIR)


def _src(**kw):
    base = {"hardware": DUAL_GPU, "tier": "auto", "model": "auto", "plugins": "auto"}
    base.update(kw)
    return Source.from_dict(base)


@pytest.fixture
def rendered(registry):
    return render(_src(), CATALOG, registry)


# --- the enforcement that matters: nothing GPU-touching ships undeclared --------------------

def test_every_gpu_reserving_plugin_service_declares_arbitration(registry):
    """A manifest service that gets a GPU device MUST say how it is arbitrated.

    This is the test that makes the schema load-bearing. `gpu: true` / `gpu_pin:` is compose
    WIRING — it hands the container a card. Without a matching `gpu_arbitration:` block the
    arbiter never learns the service exists, which is exactly the state ComfyUI was in.
    """
    undeclared = [
        f"{p.id}/{s.name}"
        for p in registry.plugins for s in p.services
        if (s.gpu or s.gpu_pin) and s.gpu_arbitration is None
    ]
    assert not undeclared, (
        f"these services reserve a GPU but declare no `gpu_arbitration:` — the scheduler cannot "
        f"arbitrate what it is not told about: {undeclared}")


def test_every_core_service_with_a_gpu_reservation_is_declared():
    """Same rule for the substrate services compose renders without a manifest."""
    rendered_compose = compose.render_compose(has_gpu=True, compose_profiles=[])
    reserving = {
        name for name, svc in rendered_compose["services"].items()
        if "deploy" in svc and "gpu" in str(svc["deploy"])
    }
    missing = reserving - set(gpu.CORE_GPU_ARBITRATION)
    assert not missing, (
        f"core services reserve a GPU but are absent from gpu.CORE_GPU_ARBITRATION: {missing}")


def test_no_service_declares_a_gpu_without_a_reservation(registry):
    """The inverse drift: a declaration whose service never actually gets a card."""
    for p in registry.plugins:
        for s in p.services:
            if s.gpu_arbitration is not None and s.gpu_arbitration.mode != "exempt":
                assert s.gpu or s.gpu_pin, (
                    f"{p.id}/{s.name} declares gpu_arbitration but renders no GPU reservation")


# --- derivation: the arbiter reads declarations, not names ----------------------------------

def test_resident_set_is_derived_and_excludes_the_secondary_card(rendered):
    residents = gpu.primary_residents(rendered.gpu_inventory())
    # llamacpp's footprint comes from the render (weights + KV at the rendered ctx), never a
    # constant — so it cannot drift from what .env tells llama-server to load.
    assert residents["llamacpp"] == pytest.approx(rendered.resident_vram_gb())
    # llamacpp-embed genuinely holds VRAM on the same card and was previously invisible.
    assert "llamacpp-embed" in residents
    # The 1070's voice models must NOT be folded into the 5090's budget.
    assert "stt" not in residents and "tts" not in residents


def test_voice_contends_on_the_secondary_device(rendered):
    by_service = {c.service: c for c in rendered.gpu_inventory()}
    assert by_service["stt"].device == "secondary"
    assert by_service["tts"].device == "secondary"
    # ...and the pin is the source of that default, so the two can never disagree.
    assert by_service["comfyui"].device == "primary"


def test_non_preemptible_residents_are_removed_from_the_budget_not_offered(rendered):
    """A resident the arbiter may not reclaim must not have its VRAM offered for admission."""
    claims = rendered.gpu_inventory()
    # The 1070's residents are non-preemptible but on the secondary device, so they contribute
    # nothing to the PRIMARY budget — the function is device-scoped, which is the point.
    assert gpu.pinned_primary_vram_gb(claims) == 0.0
    pinned = [gpu.GpuClaim(service="x", owner="t", mode="resident", enforcement="broker",
                           device="primary", vram_gb=4.0, est_seconds=0, kind="resident",
                           preemptible=False, yield_strategy="stop", degraded_service="")]
    assert gpu.pinned_primary_vram_gb(pinned) == 4.0
    assert gpu.primary_residents(pinned) == {}


def test_exclusive_resolves_to_the_whole_card_not_a_magic_number():
    """`vram_gb: exclusive` must track the hardware, so an exclusive render is exclusive on a
    16GB card too — and is never rejected as 'bigger than this GPU'."""
    arb = gpu.GpuArbitration(mode="burst", enforcement="client", vram_gb=gpu.EXCLUSIVE)
    small = HardwareProfile(gpus=(GPU(name="A", vram_gb=16.0),))
    big = HardwareProfile(gpus=(GPU(name="B", vram_gb=32.0),))
    assert arb.resolve_vram_gb(small) == 16.0
    assert arb.resolve_vram_gb(big) == 32.0
    # An exclusive request must be admissible (== total), never rejected as too big (> total).
    assert arb.resolve_vram_gb(big) <= big.primary_vram_gb


def test_an_exclusive_burst_request_evicts_every_primary_resident(rendered):
    """End-to-end through the real Scheduler: the declarations produce an actual eviction."""
    from ordo.broker import Broker, MockBackend
    from ordo.scheduler import Job, Scheduler

    claims = rendered.gpu_inventory()
    sched = Scheduler(rendered.hardware.primary_vram_gb)
    for service, vram in gpu.primary_residents(claims).items():
        sched.cache_idle(service, vram)
    backend = MockBackend()
    broker = Broker(sched, backend)

    comfy = next(c for c in claims if c.service == "comfyui")
    broker.request(Job(id="gate-comfyui", vram_gb=comfy.vram_gb, kind=comfy.kind))

    assert "gate-comfyui" in sched.running_ids, "the exclusive request was not admitted"
    assert set(backend.stopped) >= {"llamacpp", "llamacpp-embed"}, (
        f"an exclusive render must reclaim every primary resident; stopped={backend.stopped}")
    # ...and they come back when it drains — the other half of the contract.
    broker.complete("gate-comfyui")
    assert set(backend.started) >= {"llamacpp", "llamacpp-embed"}


# --- gate enforcement -----------------------------------------------------------------------

def test_gated_service_renders_its_gate_and_the_gate_never_reserves_a_gpu(rendered):
    svcs = rendered.compose_dict()["services"]
    for claim in gpu.gated_claims(rendered.gpu_inventory()):
        name = gpu.gate_service_name(claim.service)
        assert name in svcs, f"{claim.service} is gate-enforced but rendered no gate"
        gate = svcs[name]
        assert "deploy" not in gate, "the gate must reserve no GPU — it is a proxy, not a worker"
        assert gate["depends_on"] == [claim.service]
        # The gate asks for exactly what the declaration says, so the two cannot disagree.
        assert gate["environment"]["ORDO_LEASE_VRAM_GB"] == str(claim.vram_gb)
        assert gate["environment"]["ORDO_LEASE_KIND"] == claim.kind
        assert gate["environment"]["OPS_CONTROLLER_URL"] == "http://ops-controller:9000"
        # A stable job id is what lets a restarted gate clear its own stranded residency.
        assert gate["environment"]["ORDO_LEASE_JOB_ID"] == f"gate-{claim.service}"
        # It must ride the upstream's profile: a dormant service must not get a live gate, and
        # an enabled one must never come up without its arbitration.
        assert gate.get("profiles") == svcs[claim.service].get("profiles")


def test_gate_rides_the_same_port_so_consumers_only_change_hostname(rendered):
    for p, ps in rendered.plugin_services:
        arb = ps.gpu_arbitration
        if arb and arb.enforcement == "gate":
            assert arb.gate.listen_port == arb.gate.upstream_port


def test_every_gated_service_redirects_its_consumers_through_the_gate(rendered):
    """Half-redirected consumers would keep a live bypass open, which is the bug, not a nit."""
    for claim in gpu.gated_claims(rendered.gpu_inventory()):
        var = GATED_SERVICE_URL_ENV.get(claim.service)
        assert var, (f"{claim.service} is gate-enforced but has no entry in "
                     f"GATED_SERVICE_URL_ENV — its consumers would still call it directly")
        assert rendered.env[var] == f"http://{gpu.gate_service_name(claim.service)}:8188"


def test_comfyui_gates_both_prompt_paths(registry):
    """/api/prompt is the same handler behind the frontend's /api prefix. Gating only /prompt
    would leave a live, unarbitrated route to the GPU."""
    ps = next(s for p in registry.plugins if p.id == "comfyui" for s in p.services
              if s.name == "comfyui")
    assert set(ps.gpu_arbitration.gate.submit_paths) >= {"/prompt", "/api/prompt"}


def test_gate_image_has_a_resolvable_build_context():
    assert compose.SUBSTRATE_BUILD_CONTEXTS["gpu-gate"] == "services/gpu-gate"


# --- the schema refuses to overstate the runtime ---------------------------------------------

def test_schema_rejects_an_unimplemented_yield_strategy():
    """`handover` (quiesce -> reroute -> drain -> release -> ack) is not implemented. Parsing it
    would let a manifest promise a guarantee the stack does not provide."""
    with pytest.raises(gpu.GpuArbitrationError, match="not implemented"):
        gpu.GpuArbitration.from_dict({
            "mode": "resident", "enforcement": "broker", "vram_gb": 8,
            "yield": {"strategy": "handover"}})


def test_failover_must_name_its_degraded_target():
    with pytest.raises(gpu.GpuArbitrationError, match="degraded_service"):
        gpu.GpuArbitration.from_dict({
            "mode": "resident", "enforcement": "broker", "vram_gb": 8,
            "yield": {"strategy": "failover"}})


def test_mode_and_enforcement_must_be_compatible():
    # A resident has no per-unit submission to intercept.
    with pytest.raises(gpu.GpuArbitrationError, match="cannot be enforced"):
        gpu.GpuArbitration.from_dict(
            {"mode": "resident", "enforcement": "gate", "vram_gb": 8})
    # A burst service's work start is invisible to the broker.
    with pytest.raises(gpu.GpuArbitrationError, match="cannot be enforced"):
        gpu.GpuArbitration.from_dict({"mode": "burst", "enforcement": "broker", "vram_gb": 8})


def test_gate_enforcement_requires_the_facts_the_gate_needs():
    for missing, pattern in (
        ({"upstream_port": 8188, "queue_path": "/queue"}, "submit_paths"),
        ({"upstream_port": 8188, "submit_paths": ["/p"]}, "queue_path"),
        ({"submit_paths": ["/p"], "queue_path": "/queue"}, "upstream_port"),
    ):
        with pytest.raises(gpu.GpuArbitrationError, match=pattern):
            gpu.GpuArbitration.from_dict({"mode": "burst", "enforcement": "gate",
                                          "vram_gb": 8, "gate": missing})


def test_a_footprint_is_mandatory_for_anything_that_holds_vram():
    with pytest.raises(gpu.GpuArbitrationError, match="vram_gb is required"):
        gpu.GpuArbitration.from_dict({"mode": "burst", "enforcement": "client"})


# --- the yield contract is checkable, not folklore --------------------------------------------

def test_llamacpp_yields_by_migrating_to_a_real_cpu_service(registry, rendered):
    """llama.cpp does not die when it yields the card — it migrates to CPU. Assert the declared
    target actually exists and is a CPU-only deployment, so the contract can't rot."""
    claim = next(c for c in rendered.gpu_inventory() if c.service == "llamacpp")
    assert claim.yield_strategy == "failover"
    target = claim.degraded_service
    assert target == "llamacpp-cpu"
    plugin = registry.get("llamacpp-cpu")
    assert plugin is not None, "the declared failover target is not a registered service"
    assert not plugin.nvidia, "a GPU failover target that also needs the GPU is not a failover"
    svc = next(s for s in plugin.services if s.name == target)
    assert svc.gpu_arbitration is None and not svc.gpu and not svc.gpu_pin, (
        "the CPU failover target must reserve no GPU — it exists to survive GPU eviction")


def test_cpu_failover_serves_the_same_context_window_as_the_gpu_model(rendered):
    """A failover that accepts less than the primary rejects requests exactly when it is needed:
    a long conversation would break the moment the card is handed over."""
    manifest = yaml.safe_load(
        (Path(DEFAULT_PLUGINS_DIR) / "llamacpp-cpu" / "plugin.yaml").read_text(encoding="utf-8"))
    cmd = [str(c) for c in manifest["services"][0]["command"]]
    ctx_arg = cmd[cmd.index("--ctx-size") + 1]
    # `${LLAMACPP_CPU_CTX:-131072}` — the DEFAULT is what runs unless the operator overrides it.
    default_ctx = int(ctx_arg.split(":-")[1].rstrip("}")) if ":-" in ctx_arg else int(ctx_arg)
    assert default_ctx == rendered.ctx_size, (
        f"CPU failover window {default_ctx} != GPU window {rendered.ctx_size}; a swap would "
        f"break long conversations")


def test_the_failover_router_is_configured_to_route_there():
    """`degraded_via: model-gateway` is only true if LiteLLM actually declares the fallback."""
    cfg = yaml.safe_load(
        (Path(DEFAULT_PLUGINS_DIR) / "model-gateway" / "litellm_config.yaml").read_text(encoding="utf-8"))
    fallbacks = cfg["router_settings"]["fallbacks"]
    targets = [t for entry in fallbacks for t in entry.get("local-chat", [])]
    assert targets, "model-gateway declares no local-chat fallback, so the yield contract lies"
    names = {d["model_name"] for d in cfg["model_list"]}
    for t in targets:
        assert t in names, f"fallback target {t!r} is not a declared deployment"


# --- resident starvation: the failure the whole effort exists to prevent ----------------------

def test_prompts_queued_behind_a_held_lease_never_strand_the_resident(rendered):
    """OBSERVED LIVE 2026-08-10, and the reason this test exists.

    A batch job held the card legitimately. Each ComfyUI submission filed a `gate-comfyui`
    residency request that could not be admitted, and ~10 of them piled up in the queue. When the
    batch finished, ONE stale entry was admitted and kept llama.cpp evicted for work that no
    longer existed; the resident did not come back until the entries were drained by hand.

    That is availability lost with not a single render running — exactly what GPU arbitration is
    supposed to prevent. Two properties fix it and both are asserted here:
      * a residency request is IDENTITY-based, so re-asking does not stack duplicates;
      * withdrawing works on a QUEUED request, not just a running one, so a client that gave up
        takes its request with it.
    """
    from ordo.broker import Broker, MockBackend
    from ordo.scheduler import Job, Scheduler

    claims = rendered.gpu_inventory()
    sched = Scheduler(rendered.hardware.primary_vram_gb)
    for service, vram in gpu.primary_residents(claims).items():
        sched.cache_idle(service, vram)
    backend = MockBackend()
    broker = Broker(sched, backend)

    # A legitimate long batch takes the card; the resident is evicted for it, as designed.
    broker.request(Job(id="songgen-batch", vram_gb=20.0, kind="media", est_seconds=5400))
    assert "songgen-batch" in sched.running_ids
    assert "llamacpp" in backend.stopped

    # Ten prompts are submitted while it runs. The gate re-asks for residency each time.
    for _ in range(10):
        broker.request(Job(id="gate-comfyui", vram_gb=31.8, kind="media"))
    assert sched.queued_ids.count("gate-comfyui") == 1, (
        f"re-asking stacked duplicates: {sched.queued_ids}")

    # The submitters gave up (the gate withdraws on acquire timeout), then the batch finishes.
    broker.complete("gate-comfyui")
    assert "gate-comfyui" not in sched.queued_ids, "a withdrawn request stayed in the queue"
    backend.started.clear()
    broker.complete("songgen-batch")

    # THE ASSERTION THAT MATTERS: no manual intervention, resident back.
    assert sched.running_ids == [], f"work is still admitted: {sched.running_ids}"
    assert sched.queued_ids == [], f"orphaned entries survive: {sched.queued_ids}"
    assert sched.evicted_residents == {}, "a resident is still evicted with nothing running"
    assert "llamacpp" in backend.started, (
        "llama.cpp was not restarted after the real work finished — this is the resident "
        "starvation that required manual draining live")


def test_a_long_holder_can_see_its_heartbeat_obligation(rendered):
    """A real batch is ~80-90 minutes, well past LEASE_TTL_MAX (3600s), so heartbeating is a
    normal obligation and not an edge case. The arbiter must therefore TELL a holder how long
    its grant is good for, rather than each client hardcoding an assumption about the TTL."""
    from ordo.scheduler import Job, Scheduler

    sched = Scheduler(rendered.hardware.primary_vram_gb)
    sched.submit(Job(id="long-batch", vram_gb=20.0, kind="media", est_seconds=5400))
    sched.pump()
    running = sched.status()["running"][0]
    assert running["lease_ttl_s"] > 0, "a holder cannot see when its residency expires"
    assert sched.heartbeat("long-batch") is True
    # A heartbeat must actually extend past the estimate-derived cap, or a legitimate long hold
    # is force-completed mid-work and the resident reloads into contention.
    assert sched.status()["running"][0]["lease_ttl_s"] >= sched.heartbeat_ttl - 1
