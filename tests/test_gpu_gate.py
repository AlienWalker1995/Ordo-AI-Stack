"""gpu-gate behaviour against a stub arbiter and a stub upstream (real HTTP, real proxying).

The properties under test are the ones the 2026-08-08 incident turned into requirements:

  1. residency is taken BEFORE a submission reaches the upstream (proactive, not reactive) —
     this is the difference between closing the dual-tenant window and merely narrowing it;
  2. a refusal is a visible, honest 503 in the upstream's error envelope, never a silent
     pass-through — GPU work must never run unarbitrated;
  3. residency is released only when the upstream's queue has actually drained, because a
     submit call returns long before the render finishes;
  4. nothing leaks: a crash releases via TTL, a restart clears its own stranded lease, and a
     lost lease is re-acquired rather than abandoned.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

GATE_PY = Path(__file__).resolve().parents[1] / "services" / "gpu-gate" / "gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("ordo_gpu_gate", GATE_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ordo_gpu_gate"] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


# --- stubs -----------------------------------------------------------------------------------

OPS_TOKEN = "gate-test-token"


class StubOps:
    """Minimal ops-controller: admits after `admit_after` /status polls, or rejects/hangs."""

    def __init__(self, admit_after: int = 0, reject: bool = False, unreachable: bool = False):
        self.admit_after, self.reject, self.unreachable = admit_after, reject, unreachable
        self.jobs, self.completes, self.heartbeats = [], [], []
        self.polls = 0
        self.heartbeat_status = 200
        self.restarts = []            # services restarted through ops-controller
        self.restart_status = 200
        self.on_restart = None        # callback: what restarting the upstream does to it

    def _sched(self):
        jid = self.jobs[-1] if self.jobs else None
        if self.reject:
            return {"running": [], "rejected": [jid], "queued": [], "eta_seconds": None}
        admitted = self.polls >= self.admit_after
        return {"running": ([{"id": jid}] if (jid and admitted) else []),
                "rejected": [], "queued": [] if admitted else [{"id": "other"}],
                "eta_seconds": 42}

    def app(self):
        @web.middleware
        async def require_token(request, handler):
            # Same rule as ops-controller: every call carries the bearer token.
            if request.headers.get("Authorization") != f"Bearer {OPS_TOKEN}":
                return web.json_response({"error": "missing or invalid bearer token"}, status=401)
            # The gate names itself, so ops-controller's audit log records who took each lease.
            if request.headers.get("X-Actor") != "gpu-gate":
                return web.json_response({"error": "missing X-Actor"}, status=400)
            return await handler(request)

        app = web.Application(middlewares=[require_token])

        async def jobs(request):
            body = await request.json()
            self.jobs.append(body["id"])
            return web.json_response(self._sched())

        async def complete(request):
            self.completes.append((await request.json())["id"])
            return web.json_response({"running": [], "rejected": []})

        async def heartbeat(request):
            self.heartbeats.append((await request.json())["id"])
            if self.heartbeat_status != 200:
                return web.json_response({"error": "no such job"}, status=self.heartbeat_status)
            return web.json_response(self._sched())

        async def status(request):
            self.polls += 1
            return web.json_response({"gpu": self._sched()})

        app.router.add_post("/jobs", jobs)
        app.router.add_post("/jobs/complete", complete)
        app.router.add_post("/jobs/heartbeat", heartbeat)
        async def restart(request):
            service = request.match_info["service"]
            if (await request.json()).get("confirm") is not True:
                return web.json_response({"error": "confirm required"}, status=400)
            self.restarts.append(service)
            if self.restart_status != 200:
                return web.json_response({"error": "restart failed"}, status=self.restart_status)
            if self.on_restart:
                self.on_restart()
            return web.json_response({"ok": True})

        app.router.add_get("/status", status)
        app.router.add_post("/services/{service}/restart", restart)
        return app


class StubUpstream:
    """Minimal ComfyUI: /prompt records submissions and fills the queue; /queue reports it."""

    def __init__(self):
        self.prompts = []
        self.queue_depth = 0
        self.reachable = True

    def app(self):
        app = web.Application()

        async def prompt(request):
            self.prompts.append(await request.json())
            self.queue_depth += 1
            return web.json_response({"prompt_id": f"p{len(self.prompts)}", "number": 1})

        async def queue(request):
            if not self.reachable:
                return web.json_response({"error": "down"}, status=500)
            return web.json_response({
                "queue_running": [["r"]] * min(self.queue_depth, 1),
                "queue_pending": [["p"]] * max(self.queue_depth - 1, 0)})

        async def view(request):
            return web.Response(body=b"x" * 200_000, content_type="image/png")

        async def history(request):
            return web.json_response({request.match_info["pid"]: {"status": {"completed": True}}})

        async def ws(request):
            # ComfyUI pushes execution progress here; echo stands in for that stream.
            sock = web.WebSocketResponse()
            await sock.prepare(request)
            await sock.send_str(json.dumps({"type": "status", "data": {"sid": "s1"}}))
            async for msg in sock:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await sock.send_str(f"echo:{msg.data}")
            return sock

        async def dialogue_reel(request):
            # A custom-node endpoint that starts GPU work outside /prompt (services/comfyui
            # declares it as a submit path); it returns at once and renders afterwards.
            self.prompts.append(await request.json())
            self.queue_depth += 1
            return web.json_response({"ok": True, "job_id": "j1", "status": "running"})

        app.router.add_post("/prompt", prompt)
        app.router.add_post("/api/prompt", prompt)
        app.router.add_post("/dialogue_reel/render", dialogue_reel)
        app.router.add_get("/queue", queue)
        app.router.add_get("/view", view)
        app.router.add_get("/history/{pid}", history)
        app.router.add_get("/ws", ws)
        return app


async def _serve(app) -> tuple[str, web.AppRunner]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return f"http://127.0.0.1:{port}", runner


@pytest.fixture
async def harness(monkeypatch):
    """(gate_url, ops, upstream, gate_runner) with everything wired and torn down."""
    created = []

    async def build(ops: StubOps, upstream: StubUpstream, **env):
        ops_url, ops_runner = await _serve(ops.app())
        up_url, up_runner = await _serve(upstream.app())
        created.extend([ops_runner, up_runner])
        defaults = {
            "GATE_UPSTREAM": up_url, "GATE_LISTEN_PORT": "0",
            "GATE_SUBMIT_PATHS": "/prompt,/api/prompt", "GATE_SUBMIT_METHODS": "POST",
            "GATE_QUEUE_PATH": "/queue", "GATE_QUEUE_STYLE": "comfyui",
            "GATE_DRAIN_SECONDS": "0.2", "GATE_POLL_SECONDS": "0.05",
            "OPS_CONTROLLER_URL": ops_url, "OPS_CONTROLLER_TOKEN": OPS_TOKEN, "ORDO_LEASE_VRAM_GB": "30",
            "GATE_UPSTREAM_SERVICE": "comfyui",
            "ORDO_LEASE_KIND": "media", "ORDO_LEASE_JOB_ID": "gate-comfyui",
            "ORDO_LEASE_ACQUIRE_TIMEOUT_S": "2", "ORDO_LEASE_POLL_S": "0.05",
            "ORDO_LEASE_HEARTBEAT_S": "0.1",
        }
        defaults.update(env)
        for k, v in defaults.items():
            monkeypatch.setenv(k, str(v))
        cfg = gate.Config()
        assert cfg.validate() == []
        app = gate.make_app(cfg)
        url, runner = await _serve(app)
        created.append(runner)
        return url, app

    yield build
    for runner in reversed(created):
        await runner.cleanup()


# --- 1. proactive: residency before the work ------------------------------------------------

@pytest.mark.asyncio
async def test_residency_is_taken_before_the_submission_reaches_the_upstream(harness):
    """The whole point. While the arbiter withholds admission the upstream must see NOTHING —
    a reactive design would already have the render running by now."""
    ops, upstream = StubOps(admit_after=3), StubUpstream()
    url, _ = await harness(ops, upstream)
    async with aiohttp.ClientSession() as s:
        task = asyncio.create_task(s.post(f"{url}/prompt", json={"prompt": {}}))
        await asyncio.sleep(0.12)
        assert upstream.prompts == [], (
            "the upstream received the prompt before residency was granted — the gate is "
            "reactive, which is the bug")
        assert ops.jobs == ["gate-comfyui"], "no residency request was filed"
        r = await task
        assert r.status == 200
        r.release()
    assert upstream.prompts, "the prompt was never forwarded after admission"


@pytest.mark.asyncio
async def test_non_submit_traffic_is_proxied_without_taking_residency(harness):
    """Browsing the UI, polling /queue and fetching results must not evict the resident LLM."""
    ops, upstream = StubOps(), StubUpstream()
    url, _ = await harness(ops, upstream)
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/queue") as r:
            assert r.status == 200
            assert "queue_running" in await r.json()
        async with s.get(f"{url}/view") as r:      # large streamed body
            assert len(await r.read()) == 200_000
    assert ops.jobs == []


@pytest.mark.asyncio
async def test_output_fetches_and_the_progress_websocket_pass_through_ungated(harness):
    """With ComfyUI reachable ONLY through the gate, the web UI's progress stream (/ws) and every
    result fetch (/history, /view) must relay through it, or isolating the upstream breaks the UI."""
    ops, upstream = StubOps(), StubUpstream()
    url, _ = await harness(ops, upstream)
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/history/p1") as r:
            assert r.status == 200 and (await r.json())["p1"]["status"]["completed"] is True
        async with s.ws_connect(f"{url}/ws?clientId=c1") as sock:
            first = await sock.receive_json(timeout=5)
            assert first["type"] == "status"
            await sock.send_str("ping")
            assert (await sock.receive(timeout=5)).data == "echo:ping"
    assert ops.jobs == [], "browsing and progress streaming must not take the GPU"


@pytest.mark.asyncio
async def test_a_declared_custom_submit_path_takes_residency_first(harness):
    """Submit paths come from the manifest, not code: a custom endpoint that launches a render
    (ComfyUI's /dialogue_reel/render) is gated exactly like /prompt once it is declared."""
    ops, upstream = StubOps(admit_after=3), StubUpstream()
    url, _ = await harness(ops, upstream,
                           GATE_SUBMIT_PATHS="/prompt,/api/prompt,/dialogue_reel/render")
    async with aiohttp.ClientSession() as s:
        task = asyncio.create_task(s.post(f"{url}/dialogue_reel/render", json={"dialogue": {}}))
        await asyncio.sleep(0.12)
        assert upstream.prompts == [], "the render started before residency was granted"
        r = await task
        assert r.status == 200
        r.release()
    assert ops.jobs == ["gate-comfyui"] and upstream.prompts


@pytest.mark.asyncio
async def test_the_api_prefixed_submit_path_is_gated_too(harness):
    """/api/prompt is the same handler behind the frontend prefix — an ungated alias would be a
    live bypass straight to the GPU."""
    ops, upstream = StubOps(), StubUpstream()
    url, _ = await harness(ops, upstream)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/api/prompt", json={"prompt": {}}) as r:
            assert r.status == 200
    assert ops.jobs == ["gate-comfyui"]


# --- 2. denial is visible and never silently permissive --------------------------------------

@pytest.mark.asyncio
async def test_rejection_refuses_the_submission_visibly(harness):
    ops, upstream = StubOps(reject=True), StubUpstream()
    url, _ = await harness(ops, upstream)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
            assert r.status == 503
            body = await r.json()
    # The upstream's own error envelope, so the web UI renders it in its normal error dialog.
    assert body["error"]["type"] == "gpu_residency_denied"
    assert "does not fit" in body["error"]["message"]
    assert body["node_errors"] == {}
    assert upstream.prompts == [], "work was forwarded despite being refused the GPU"


@pytest.mark.asyncio
async def test_acquire_timeout_refuses_with_an_actionable_message(harness):
    ops, upstream = StubOps(admit_after=10_000), StubUpstream()
    url, _ = await harness(ops, upstream, ORDO_LEASE_ACQUIRE_TIMEOUT_S="0.3")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
            assert r.status == 503
            body = await r.json()
    assert "Timed out" in body["error"]["message"]
    # It must say what to do, and be explicit that nothing was silently queued.
    assert "resubmit" in body["error"]["details"].lower()
    assert upstream.prompts == []


@pytest.mark.asyncio
async def test_unreachable_arbiter_fails_closed(harness):
    """No arbiter means no permission. Failing open here is what killed the machine."""
    ops, upstream = StubOps(), StubUpstream()
    url, _ = await harness(ops, upstream, OPS_CONTROLLER_URL="http://127.0.0.1:9")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
            assert r.status == 503
            body = await r.json()
    assert "unreachable" in body["error"]["message"].lower()
    assert upstream.prompts == [], "work ran while the GPU arbiter was unreachable"


# --- 3. release tracks the real queue, not the HTTP response ---------------------------------

@pytest.mark.asyncio
async def test_residency_is_held_until_the_queue_drains(harness):
    """A submit returns immediately; the render runs afterwards. Releasing on the response
    would hand the card back mid-render."""
    ops, upstream = StubOps(), StubUpstream()
    url, app = await harness(ops, upstream)
    residency = app[gate.RESIDENCY]
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
            assert r.status == 200
    assert residency.held
    await asyncio.sleep(0.4)                     # queue still non-empty -> still held
    assert residency.held, "residency was released while work was still queued"
    assert ops.heartbeats, "a held residency must be heartbeated, or its TTL sweeps it away"
    upstream.queue_depth = 0                     # render finishes
    await asyncio.sleep(0.6)                     # drain window elapses
    assert not residency.held
    # completes[0] is the startup clear of a possibly-stranded lease; this is the real release.
    assert ops.completes == ["gate-comfyui", "gate-comfyui"]


@pytest.mark.asyncio
async def test_back_to_back_submissions_share_one_residency(harness):
    """The upstream drains its OWN queue once it holds the card. Re-queuing per prompt would
    make each one a peer of unrelated GPU work and thrash the card."""
    ops, upstream = StubOps(), StubUpstream()
    url, _ = await harness(ops, upstream)
    async with aiohttp.ClientSession() as s:
        for _ in range(3):
            async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
                assert r.status == 200
    assert ops.jobs == ["gate-comfyui"], f"filed {len(ops.jobs)} residency requests, expected 1"
    assert len(upstream.prompts) == 3


@pytest.mark.asyncio
async def test_unreachable_upstream_drains_instead_of_pinning_the_card(harness):
    """If we cannot see the upstream we must give the GPU back, not hold it forever."""
    ops, upstream = StubOps(), StubUpstream()
    url, app = await harness(ops, upstream)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
            assert r.status == 200
    assert app[gate.RESIDENCY].held
    upstream.reachable = False
    await asyncio.sleep(0.6)
    assert not app[gate.RESIDENCY].held
    assert ops.completes[-1] == "gate-comfyui"   # released, not held on an invisible upstream


# --- 4. nothing leaks ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_clears_a_lease_stranded_by_a_previous_incarnation(harness):
    """A stable job id plus an unconditional release at boot means a crashed gate's lease is
    reclaimed on restart instead of waiting out the scheduler TTL."""
    ops, upstream = StubOps(), StubUpstream()
    await harness(ops, upstream)
    await asyncio.sleep(0.05)
    assert ops.completes == ["gate-comfyui"]


@pytest.mark.asyncio
async def test_startup_adopts_the_lease_when_the_upstream_is_still_rendering(harness):
    """A gate restarted mid-render must not release the card: the render still holds VRAM, and
    the release would restore the resident LLM beside it. It re-files instead, which the
    scheduler treats as the same request (idempotent on the job id), and holds residency."""
    ops, upstream = StubOps(), StubUpstream()
    upstream.queue_depth = 1
    _url, app = await harness(ops, upstream)
    await asyncio.sleep(0.05)
    assert ops.completes == []
    assert ops.jobs[:1] == ["gate-comfyui"]
    assert app[gate.RESIDENCY].held


@pytest.mark.asyncio
async def test_clean_shutdown_releases_residency(harness):
    ops, upstream = StubOps(), StubUpstream()
    url, app = await harness(ops, upstream)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
            assert r.status == 200
    assert app[gate.RESIDENCY].held
    await app.cleanup()
    assert ops.completes[-1] == "gate-comfyui"


@pytest.mark.asyncio
async def test_a_lost_lease_is_reacquired_not_abandoned(harness):
    """If the arbiter restarts and forgets us, the resident would be restored into VRAM the
    render is still using. Re-file rather than carry on unarbitrated."""
    ops, upstream = StubOps(), StubUpstream()
    url, app = await harness(ops, upstream)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
            assert r.status == 200
    ops.heartbeat_status = 404
    await asyncio.sleep(0.4)
    assert app[gate.RESIDENCY].stats["reacquired_after_loss"] >= 1
    assert ops.jobs.count("gate-comfyui") >= 2


@pytest.mark.asyncio
async def test_backstop_acquires_when_work_bypasses_the_gate_and_records_it(harness):
    """Work that reached the upstream directly is still caught — late, loudly, and counted.
    This narrows the bypass window; only routing every caller through the gate closes it."""
    ops, upstream = StubOps(), StubUpstream()
    _url, app = await harness(ops, upstream)
    upstream.queue_depth = 1                     # someone submitted around the gate
    await asyncio.sleep(0.3)
    assert app[gate.RESIDENCY].held
    assert app[gate.RESIDENCY].stats["bypass_detected"] >= 1
    assert ops.jobs == ["gate-comfyui"]


# --- config refuses to start half-armed -------------------------------------------------------

@pytest.mark.parametrize("drop,expect", [
    ("GATE_UPSTREAM", "GATE_UPSTREAM"),
    ("OPS_CONTROLLER_URL", "OPS_CONTROLLER_URL"),
    ("OPS_CONTROLLER_TOKEN", "OPS_CONTROLLER_TOKEN"),
    ("GATE_UPSTREAM_SERVICE", "GATE_UPSTREAM_SERVICE"),
    ("ORDO_LEASE_VRAM_GB", "ORDO_LEASE_VRAM_GB"),
    ("ORDO_LEASE_JOB_ID", "ORDO_LEASE_JOB_ID"),
    ("GATE_SUBMIT_PATHS", "nothing would be gated"),
    ("GATE_QUEUE_PATH", "never be released"),
])
def test_gate_refuses_to_start_half_armed(monkeypatch, drop, expect):
    # NB: keep this name under 35 characters after the `test_` prefix. TruffleHog's Lob detector
    # matches `test_` + exactly 35 word characters, and its verifier returns a FALSE POSITIVE on
    # such a match — which fails the repo's secret-scanning gate on a test function name.
    """A gate that boots without the facts it needs would proxy happily and arbitrate nothing —
    worse than being absent, because the topology would claim the traffic is gated."""
    env = {"GATE_UPSTREAM": "http://u:1", "OPS_CONTROLLER_URL": "http://o:2", "OPS_CONTROLLER_TOKEN": "t",
           "GATE_UPSTREAM_SERVICE": "comfyui",
           "ORDO_LEASE_VRAM_GB": "30", "ORDO_LEASE_JOB_ID": "gate-x",
           "GATE_SUBMIT_PATHS": "/prompt", "GATE_QUEUE_PATH": "/queue"}
    for k, v in env.items():
        monkeypatch.setenv(k, "" if k == drop else v)
    problems = gate.Config().validate()
    assert any(expect in p for p in problems), problems


def test_queue_readers_cover_the_declared_styles():
    assert gate.QUEUE_READERS["comfyui"](
        {"queue_running": [1], "queue_pending": [2, 3]}) == 3
    assert gate.QUEUE_READERS["comfyui"]({"queue_running": [], "queue_pending": []}) == 0
    assert gate.QUEUE_READERS["comfyui"](json.loads("null")) == 0


@pytest.mark.asyncio
async def test_a_refused_submission_withdraws_its_queued_request(harness):
    """OBSERVED LIVE 2026-08-10: ten `gate-comfyui` requests piled up behind a legitimately-held
    batch lease, and one was later admitted — keeping llama.cpp evicted for a prompt that was
    refused and never submitted. Abandoning a queued request is a lease leak with a delayed
    fuse, so a gate that gives up must take its request back out of the arbiter's queue."""
    ops, upstream = StubOps(admit_after=10_000), StubUpstream()
    url, _ = await harness(ops, upstream, ORDO_LEASE_ACQUIRE_TIMEOUT_S="0.3")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
            assert r.status == 503
    # completes[0] is the startup clear; the second is the withdrawal of the abandoned request.
    assert ops.completes == ["gate-comfyui", "gate-comfyui"], (
        f"the refused request was not withdrawn: completes={ops.completes}")
    assert upstream.prompts == []


@pytest.mark.asyncio
async def test_a_wedged_upstream_is_restarted_before_the_card_is_released(harness):
    """The one failure the scheduler's TTL cannot catch: the gate is alive and heartbeating while
    the upstream makes no progress. Releasing then would restore the resident LLM beside a render
    that still holds VRAM (two tenants: the 2026-08-08 host crash), so the gate restarts the
    upstream first and releases only once it is idle."""
    ops, upstream = StubOps(), StubUpstream()
    ops.on_restart = lambda: setattr(upstream, "queue_depth", 0)   # a restart clears the queue
    url, app = await harness(ops, upstream, GATE_MAX_HOLD_SECONDS="0.5", GATE_DRAIN_SECONDS="600")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
            assert r.status == 200
    assert app[gate.RESIDENCY].held
    upstream.queue_depth = 1          # never drains on its own: wedged
    await asyncio.sleep(1.2)
    assert ops.restarts == ["comfyui"], "the wedged upstream was not restarted"
    assert not app[gate.RESIDENCY].held, "the card was never released after the restart"
    assert app[gate.RESIDENCY].stats["max_hold_expired"] == 1
    assert ops.completes[-1] == "gate-comfyui"


async def test_a_wedged_upstream_that_cannot_be_restarted_keeps_the_card(harness):
    """If the upstream cannot be stopped, releasing would put the resident beside it. Holding is
    the safe failure: chat stays on the CPU fallback and the alarm says what needs a person."""
    ops, upstream = StubOps(), StubUpstream()
    ops.restart_status = 500
    url, app = await harness(ops, upstream, GATE_MAX_HOLD_SECONDS="0.5", GATE_DRAIN_SECONDS="600")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/prompt", json={"prompt": {}}) as r:
            assert r.status == 200
    completes_before = len(ops.completes)     # the startup stranded-lease clear counts as one
    upstream.queue_depth = 1
    await asyncio.sleep(1.2)
    assert ops.restarts, "no restart was attempted"
    assert app[gate.RESIDENCY].held, "released the card while the upstream may still hold VRAM"
    assert len(ops.completes) == completes_before, "the lease was completed"
