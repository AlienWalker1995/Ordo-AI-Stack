"""Ordo GPU admission gate — a generic reverse proxy that takes GPU residency before it lets
work through.

WHY THIS EXISTS
    A long-running GPU server with a submission API (ComfyUI is the canonical one) cannot
    arbitrate itself. It cannot take residency at start — it would hold the card forever while
    idle. It cannot be trusted to take residency per job — the work is submitted by whoever is
    holding the mouse, and the node-graph web UI has never heard of the scheduler. On 2026-08-08
    that gap let a hand-queued render share the 5090 with the resident llama.cpp for ~4 hours at
    ~98% VRAM until the Windows graphics kernel bugchecked (0x113).

    The obvious fix — poll the server's queue and take residency when it goes non-empty — does
    not close the hole. ComfyUI begins executing the instant Queue Prompt is pressed, so a poller
    acquires *after* the render is already running on a card that still holds the LLM. It narrows
    the dual-tenant window; it does not remove it, and the window is the failure mode.

    So this gate is PROACTIVE: it sits in front of the submission API, and a request that starts
    GPU work does not reach the server until residency has been granted. Nothing races.

WHAT IT IS NOT
    It is not an arbiter. It never inspects VRAM, never decides who wins, and never starts or
    stops another container. It is a CLIENT of ops-controller — `POST /jobs`, `/jobs/heartbeat`,
    `/jobs/complete` — speaking the same env contract as `assets/lease-exec.py`. Two things
    racing to arbitrate one card is the documented deadlock that got the old ComfyUI guardian
    retired; there is exactly one arbiter and this is not it.

THE TWO HALVES
    Acquire is synchronous with the submission; RELEASE cannot be. A submit call returns as soon
    as the prompt is accepted and the render runs afterwards, so the HTTP response is not the
    finish signal. The gate therefore also polls the upstream's queue endpoint and releases
    residency only once the queue has been empty for `GATE_DRAIN_SECONDS` — which also stops it
    flapping the card between back-to-back prompts.

    The same loop is a BACKSTOP: if it ever sees the upstream busy while the gate holds nothing,
    something reached the server without passing through here, and it acquires immediately and
    records the bypass at `/_gpu_gate/status`. That path is reactive by nature — it narrows the
    window, it cannot close it — so it is a safety net and an alarm, never the mechanism. The
    mechanism is that every route to the server goes through the gate.

FAILURE BEHAVIOUR (all deliberate)
    * Arbiter unreachable, or residency refused/timed out -> the submission is REFUSED with 503
      and a human-readable message in the upstream's own error envelope, so the web UI shows it
      in its normal error dialog. It never proceeds unleased. GPU work running without residency
      is the thing that broke the machine; a failed Queue Prompt is not.
    * Gate crashes or is killed mid-render -> it stops heartbeating, and the scheduler's lease
      TTL sweep force-completes the job and restores the evicted resident. No lease is leaked and
      llama.cpp cannot be stranded off the card. Worst-case downtime is one heartbeat TTL.
    * Gate restarts -> it uses a STABLE job id (`ORDO_LEASE_JOB_ID`) and unconditionally releases
      that id at startup, so a lease stranded by its previous incarnation is cleared immediately
      rather than waiting out the TTL.
    * Arbiter restarts and forgets the lease -> a heartbeat 404s, and the gate re-acquires rather
      than letting the resident be restored into VRAM the render is still using.
    * Upstream unreachable while polling -> treated as NOT busy, so a dead upstream drains and
      releases the card instead of pinning it.
    * Upstream WEDGED (alive, still reports outstanding work, making no progress) -> the one case
      the scheduler's TTL cannot catch, because a live gate keeps heartbeating. Capped by
      GATE_MAX_HOLD_SECONDS: residency is released and the condition logged as an alarm, because
      stranding the resident LLM off the card indefinitely is not an acceptable failure mode.

CONFIG (env; the ORDO_LEASE_* names are shared verbatim with assets/lease-exec.py so there is one
vocabulary for asking the arbiter for the GPU):
    GATE_UPSTREAM                 base URL of the service being fronted
    GATE_LISTEN_PORT              port this gate listens on (usually the upstream's, drop-in)
    GATE_SUBMIT_PATHS             comma-separated paths that start GPU work
    GATE_SUBMIT_METHODS           comma-separated methods (default POST)
    GATE_QUEUE_PATH               path polled to tell whether work is outstanding
    GATE_QUEUE_STYLE              how to read that JSON (see QUEUE_READERS)
    GATE_DRAIN_SECONDS            queue must read empty this long before residency is released
    GATE_MAX_HOLD_SECONDS         cap on one continuous hold (wedged-upstream guard; 0 disables)
    GATE_POLL_SECONDS             queue poll interval
    OPS_CONTROLLER_URL            the arbiter (required)
    OPS_CONTROLLER_TOKEN          optional bearer
    ORDO_LEASE_VRAM_GB            residency footprint to request (required)
    ORDO_LEASE_KIND               job kind label
    ORDO_LEASE_JOB_ID             STABLE job id for this gate
    ORDO_LEASE_EST_SECONDS        duration hint
    ORDO_LEASE_ACQUIRE_TIMEOUT_S  how long a submitter waits before an honest refusal
    ORDO_LEASE_POLL_S             admission poll interval
    ORDO_LEASE_HEARTBEAT_S        heartbeat interval while residency is held
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from typing import Any

import aiohttp
from aiohttp import web

LOG = logging.getLogger("gpu-gate")

# Typed application keys (aiohttp's supported way to stash per-app state; bare string keys are
# deprecated). Declared after the classes they reference — see the bottom of this module.

# Reserved prefix for the gate's own endpoints. Namespaced so it cannot collide with an upstream
# route (ComfyUI owns /queue, /prompt, /history, /view, …).
CONTROL_PREFIX = "/_gpu_gate"

# Hop-by-hop headers that must not be forwarded across a proxy hop (RFC 9110 §7.6.1).
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_f(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        LOG.warning("%s=%r is not a number — using %s", name, raw, default)
        return default


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(p.strip() for p in _env(name, default).split(",") if p.strip())


# --- upstream queue readers -----------------------------------------------------------------
# "Is there outstanding GPU work?" is the one service-specific question the gate has to ask, so
# it is a small named table rather than a hardcoded assumption. A new gated service adds a reader
# here (and declares `gate.queue_style` in its manifest) instead of forking the gate.

def _read_comfyui_queue(payload: Any) -> int:
    """ComfyUI GET /queue -> {"queue_running": [...], "queue_pending": [...]}."""
    if not isinstance(payload, dict):
        return 0
    return len(payload.get("queue_running") or []) + len(payload.get("queue_pending") or [])


def _read_count_queue(payload: Any) -> int:
    """A generic {"count": N} / {"pending": N, "running": N} shape."""
    if not isinstance(payload, dict):
        return 0
    if "count" in payload:
        return int(payload.get("count") or 0)
    return int(payload.get("running") or 0) + int(payload.get("pending") or 0)


QUEUE_READERS = {"comfyui": _read_comfyui_queue, "count": _read_count_queue}


class Config:
    def __init__(self) -> None:
        self.upstream = _env("GATE_UPSTREAM").rstrip("/")
        self.listen_port = int(_env_f("GATE_LISTEN_PORT", 8188))
        self.submit_paths = _csv("GATE_SUBMIT_PATHS")
        self.submit_methods = frozenset(m.upper() for m in _csv("GATE_SUBMIT_METHODS", "POST"))
        self.queue_path = _env("GATE_QUEUE_PATH")
        self.queue_style = _env("GATE_QUEUE_STYLE", "comfyui")
        self.drain_seconds = _env_f("GATE_DRAIN_SECONDS", 60.0)
        # Upper bound on one continuous residency. Defends against a WEDGED upstream, which is
        # the one failure the scheduler's TTL cannot catch (a live gate keeps heartbeating). Set
        # far above any legitimate render; 0 disables the cap.
        self.max_hold_seconds = _env_f("GATE_MAX_HOLD_SECONDS", 3600.0)
        self.poll_seconds = _env_f("GATE_POLL_SECONDS", 5.0)
        self.ops_url = _env("OPS_CONTROLLER_URL").rstrip("/")
        self.ops_token = _env("OPS_CONTROLLER_TOKEN")
        self.vram_gb = _env_f("ORDO_LEASE_VRAM_GB", 0.0)
        self.kind = _env("ORDO_LEASE_KIND", "media")
        self.job_id = _env("ORDO_LEASE_JOB_ID")
        self.est_seconds = _env_f("ORDO_LEASE_EST_SECONDS", 0.0)
        self.acquire_timeout = _env_f("ORDO_LEASE_ACQUIRE_TIMEOUT_S", 120.0)
        self.acquire_poll = _env_f("ORDO_LEASE_POLL_S", 2.0)
        self.heartbeat_seconds = _env_f("ORDO_LEASE_HEARTBEAT_S", 60.0)

    def validate(self) -> list[str]:
        """Refuse to start misconfigured. A gate that boots with no arbiter URL or no footprint
        would proxy happily and arbitrate nothing — worse than not being deployed, because the
        topology would claim the work is gated."""
        bad = []
        if not self.upstream:
            bad.append("GATE_UPSTREAM is required")
        if not self.ops_url:
            bad.append("OPS_CONTROLLER_URL is required")
        if self.vram_gb <= 0:
            bad.append("ORDO_LEASE_VRAM_GB must be > 0")
        if not self.job_id:
            bad.append("ORDO_LEASE_JOB_ID is required (a STABLE id, so a restart can clear its "
                       "own stranded lease)")
        if not self.submit_paths:
            bad.append("GATE_SUBMIT_PATHS is required — with none, nothing would be gated")
        if not self.queue_path:
            bad.append("GATE_QUEUE_PATH is required — without it residency could never be "
                       "released, because a submit response does not mean the work finished")
        if self.queue_style not in QUEUE_READERS:
            bad.append(f"GATE_QUEUE_STYLE={self.queue_style!r} unknown; "
                       f"known: {sorted(QUEUE_READERS)}")
        return bad


class LeaseDenied(Exception):
    """Residency was refused, timed out, or the arbiter could not be reached."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class Residency:
    """The gate's single lease on the GPU, held on behalf of the upstream service.

    One lease with a stable id, not one per prompt: the upstream drains its OWN queue once it has
    the card. Queuing every prompt separately with the arbiter would make each one a peer of a
    four-minute render from somewhere else and interleave them onto the same GPU — the opposite
    of what residency is for.
    """

    def __init__(self, cfg: Config, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.session = session
        self.held = False
        self._lock = asyncio.Lock()
        self._last_busy = 0.0
        self._last_heartbeat = 0.0
        self._held_since_mono: float | None = None
        # Set when a hold is capped out. Suppresses the BACKSTOP only — without it the backstop
        # would re-acquire on the very next poll (the wedged queue is still non-empty) and the
        # cap would accomplish nothing. Cleared once the upstream's queue actually reads empty.
        # A fresh submission through the gate is NOT suppressed: that is a new, intentional
        # request from a caller who is waiting for an answer, not an inference about old work.
        self._backstop_suppressed = False
        self.stats = {
            "acquired": 0, "released": 0, "denied": 0, "bypass_detected": 0,
            "reacquired_after_loss": 0, "max_hold_expired": 0, "held_since": None,
            "last_error": "",
        }

    # --- arbiter HTTP -------------------------------------------------------------------
    async def _ops(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.cfg.ops_token:
            headers["Authorization"] = f"Bearer {self.cfg.ops_token}"
        async with self.session.request(method, self.cfg.ops_url + path, json=body,
                                        headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=30)) as r:
            text = await r.text()
            if r.status >= 400:
                raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status,
                                                  message=text[:300])
            return json.loads(text) if text else {}

    @staticmethod
    def _gpu(payload: dict) -> dict:
        """GET /status nests the scheduler block under "gpu"; POST /jobs* returns it bare."""
        return payload.get("gpu", payload)

    def _is_running(self, status: dict) -> bool:
        return self.cfg.job_id in [j.get("id") for j in status.get("running", [])]

    # --- lifecycle ----------------------------------------------------------------------
    async def clear_stranded(self) -> None:
        """Release this gate's stable job id at startup.

        If a previous incarnation died holding residency, its lease is still charged to this id
        and the resident it evicted is still down. Completing it here reclaims that immediately
        instead of waiting out the scheduler's TTL. Harmless when nothing is stranded — the
        scheduler pops an unknown id and returns its status unchanged.
        """
        try:
            await self._ops("POST", "/jobs/complete", {"id": self.cfg.job_id})
            LOG.info("startup: cleared any stranded residency for job id %r", self.cfg.job_id)
        except (aiohttp.ClientError, OSError, ValueError) as e:
            # Not fatal: the TTL sweep is the backstop. But it means the arbiter is unreachable,
            # which the first submission will discover and refuse on.
            LOG.warning("startup: could not clear stranded residency (%s)", e)

    async def acquire(self, *, reason: str) -> None:
        """Take residency, waiting up to the configured timeout. Raises LeaseDenied.

        Serialized: concurrent submissions must not each file a request. The second waiter finds
        residency already held and proceeds — the upstream will serve both from the one grant.
        """
        async with self._lock:
            self._last_busy = time.monotonic()
            if self.held:
                return
            # An explicit submission clears a wedge suppression: someone is actively asking.
            self._backstop_suppressed = False
            deadline = time.monotonic() + self.cfg.acquire_timeout
            try:
                status = self._gpu(await self._ops("POST", "/jobs", {
                    "id": self.cfg.job_id, "vram_gb": self.cfg.vram_gb,
                    "kind": self.cfg.kind, "est_seconds": self.cfg.est_seconds,
                }))
            except (aiohttp.ClientError, OSError, ValueError) as e:
                self.stats["denied"] += 1
                self.stats["last_error"] = f"arbiter unreachable: {e}"
                raise LeaseDenied(
                    "The GPU scheduler is unreachable, so this request cannot be given the GPU.",
                    f"POST {self.cfg.ops_url}/jobs failed: {e}. Work is refused rather than run "
                    f"unarbitrated — an unscheduled render can collide with the resident LLM on "
                    f"the same card.") from e
            while True:
                if self._is_running(status):
                    self.held = True
                    self.stats["acquired"] += 1
                    self.stats["held_since"] = time.time()
                    self._held_since_mono = time.monotonic()
                    self._last_heartbeat = time.monotonic()
                    self._last_busy = time.monotonic()
                    LOG.info("residency GRANTED (%.1fGB, kind=%s) — %s",
                             self.cfg.vram_gb, self.cfg.kind, reason)
                    return
                if self.cfg.job_id in (status.get("rejected") or []):
                    self.stats["denied"] += 1
                    raise LeaseDenied(
                        f"The GPU scheduler refused this work: {self.cfg.vram_gb:.1f}GB does not "
                        f"fit on this GPU.",
                        "The request was rejected outright, not queued. Reduce the declared "
                        "footprint (gpu_arbitration.vram_gb) or run it on a larger card.")
                if time.monotonic() >= deadline:
                    self.stats["denied"] += 1
                    q = status.get("queued") or []
                    eta = status.get("eta_seconds")
                    # WITHDRAW before giving up. The request is still sitting in the arbiter's
                    # queue; left there it would be admitted later — evicting the resident LLM
                    # for a render that was refused and never submitted, with nothing to
                    # heartbeat or complete it. Abandoning a queued request is a lease leak with
                    # a delayed fuse (observed live 2026-08-10 before this was added).
                    await self._withdraw("acquire timed out")
                    raise LeaseDenied(
                        f"Timed out after {self.cfg.acquire_timeout:.0f}s waiting for the GPU — "
                        f"it is busy with other work.",
                        f"{len(q)} job(s) ahead in the scheduler queue"
                        + (f"; next free slot in ~{eta:.0f}s" if isinstance(eta, int | float)
                           else "")
                        + ". Nothing was queued on the server — resubmit when the GPU frees up.")
                await asyncio.sleep(self.cfg.acquire_poll)
                try:
                    status = self._gpu(await self._ops("GET", "/status"))
                except (aiohttp.ClientError, OSError, ValueError) as e:
                    self.stats["last_error"] = f"status poll failed: {e}"
                    LOG.warning("status poll failed while waiting for residency: %s", e)

    async def _withdraw(self, reason: str) -> None:
        """Take our request back out of the arbiter's queue. Caller already holds the lock."""
        try:
            await self._ops("POST", "/jobs/complete", {"id": self.cfg.job_id})
            LOG.info("residency request WITHDRAWN — %s", reason)
        except (aiohttp.ClientError, OSError, ValueError) as e:
            LOG.warning("withdraw failed (%s) — the request may still be queued; the "
                        "scheduler's TTL sweep is the backstop", e)
            self.stats["last_error"] = f"withdraw failed: {e}"

    async def release(self, *, reason: str) -> None:
        async with self._lock:
            if not self.held:
                return
            try:
                await self._ops("POST", "/jobs/complete", {"id": self.cfg.job_id})
                LOG.info("residency RELEASED — %s", reason)
            except (aiohttp.ClientError, OSError, ValueError) as e:
                # Do not retry forever and do not keep `held` True: if the arbiter cannot be
                # told, its TTL sweep will reclaim the lease. Pretending we still hold it would
                # stop us ever asking again.
                LOG.warning("release failed (%s) — the scheduler's TTL sweep will reclaim it", e)
                self.stats["last_error"] = f"release failed: {e}"
            self.held = False
            self._held_since_mono = None
            self.stats["released"] += 1
            self.stats["held_since"] = None

    async def heartbeat(self) -> None:
        """Renew residency. A 404 means the arbiter forgot the lease (e.g. it restarted); re-file
        it, or the resident it evicted gets restored into VRAM the render is still using."""
        try:
            await self._ops("POST", "/jobs/heartbeat", {"id": self.cfg.job_id})
        except aiohttp.ClientResponseError as e:
            if e.status != 404:
                LOG.warning("heartbeat failed: HTTP %s", e.status)
                return
            LOG.warning("residency lost (404) — re-acquiring")
            self.stats["reacquired_after_loss"] += 1
            with contextlib.suppress(aiohttp.ClientError, OSError, ValueError):
                await self._ops("POST", "/jobs", {
                    "id": self.cfg.job_id, "vram_gb": self.cfg.vram_gb,
                    "kind": self.cfg.kind, "est_seconds": self.cfg.est_seconds})
        except (aiohttp.ClientError, OSError, ValueError) as e:
            LOG.warning("heartbeat failed: %s", e)

    # --- upstream queue -----------------------------------------------------------------
    async def upstream_busy(self) -> bool | None:
        """Outstanding work upstream? None when the upstream can't be reached.

        Unreachable is deliberately NOT treated as busy: a dead upstream must drain and give the
        card back, not hold it because we cannot see it.
        """
        try:
            async with self.session.get(self.cfg.upstream + self.cfg.queue_path,
                                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status >= 400:
                    return None
                payload = await r.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError):
            return None
        return QUEUE_READERS[self.cfg.queue_style](payload) > 0

    async def watch(self) -> None:
        """Hold-and-release loop: heartbeat while work is outstanding, release once it drains,
        and acquire defensively if work appears that never came through the gate."""
        while True:
            await asyncio.sleep(self.cfg.poll_seconds)
            try:
                busy = await self.upstream_busy()
                now = time.monotonic()
                if busy:
                    self._last_busy = now
                    if not self.held and not self._backstop_suppressed:
                        # Work reached the upstream without passing through the gate. This is
                        # reactive — the render has already started — so it narrows the window
                        # rather than closing it. Loud, counted, and never the primary path.
                        self.stats["bypass_detected"] += 1
                        LOG.error("BYPASS: upstream is busy but the gate holds no residency — "
                                  "something submitted work without passing through the gate. "
                                  "Acquiring now (late), total bypasses: %d",
                                  self.stats["bypass_detected"])
                        with contextlib.suppress(LeaseDenied):
                            await self.acquire(reason="backstop: unsubmitted work detected")
                if self.held:
                    held_for = now - (self._held_since_mono or now)
                    if self.cfg.max_hold_seconds and held_for >= self.cfg.max_hold_seconds:
                        # A wedged upstream is the one case heartbeating makes WORSE: the gate is
                        # alive and faithfully renewing residency for work that will never
                        # finish, so the scheduler's TTL — which exists for a dead client — never
                        # fires and the resident LLM stays off the card indefinitely. That
                        # violates the hard rule that nothing may strand the resident, so the
                        # hold is capped. This is a genuine trade-off, not a clean win: releasing
                        # lets the resident back onto a card that may still have a wedged render
                        # on it. The cap is therefore set far above any legitimate render and its
                        # expiry is an ALARM — the real fix for a wedged upstream is to notice
                        # and restart it, which this log is what makes possible.
                        self.stats["max_hold_expired"] += 1
                        self._backstop_suppressed = True
                        LOG.error("MAX HOLD EXCEEDED: residency held %.0fs (cap %.0fs) while "
                                  "%s still reports outstanding work. Releasing so the resident "
                                  "is not stranded off the card — the upstream is very likely "
                                  "WEDGED and needs attention.",
                                  held_for, self.cfg.max_hold_seconds, self.cfg.upstream)
                        await self.release(reason="max hold exceeded (upstream likely wedged)")
                        continue
                    if now - self._last_heartbeat >= self.cfg.heartbeat_seconds:
                        await self.heartbeat()
                        self._last_heartbeat = now
                    if not busy and (now - self._last_busy) >= self.cfg.drain_seconds:
                        await self.release(reason=f"queue empty for "
                                                  f"{self.cfg.drain_seconds:.0f}s")
                if busy is False and self._backstop_suppressed:
                    # The wedge cleared on its own (or was cleared by hand) — re-arm the backstop.
                    self._backstop_suppressed = False
                    LOG.info("upstream queue drained — backstop re-armed")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the watcher must never die silently
                LOG.exception("watch loop error: %s", e)

    def status(self) -> dict:
        return {
            "held": self.held,
            "job_id": self.cfg.job_id,
            "vram_gb": self.cfg.vram_gb,
            "kind": self.cfg.kind,
            "upstream": self.cfg.upstream,
            "max_hold_seconds": self.cfg.max_hold_seconds,
            "backstop_suppressed": self._backstop_suppressed,
            "submit_paths": list(self.cfg.submit_paths),
            "submit_methods": sorted(self.cfg.submit_methods),
            **self.stats,
        }


# --- proxy ----------------------------------------------------------------------------------

def _is_submit(cfg: Config, request: web.Request) -> bool:
    if request.method.upper() not in cfg.submit_methods:
        return False
    path = request.path.rstrip("/") or "/"
    return any(path == p.rstrip("/") or path == p for p in cfg.submit_paths)


def _denied_response(e: LeaseDenied) -> web.Response:
    """Refusal in the upstream's own error envelope.

    ComfyUI's frontend renders a non-2xx /prompt body's `error` object in its normal error
    dialog, so the person who pressed Queue Prompt sees exactly why the GPU was refused instead
    of a silent hang or a generic failure. 503 (not 500): this is "try again", not "broken".
    """
    return web.json_response(status=503, data={
        "error": {
            "type": "gpu_residency_denied",
            "message": e.message,
            "details": e.detail,
            "extra_info": {"gate": "ordo gpu-gate",
                           "why": "all GPU work on this stack takes a scheduler lease"},
        },
        "node_errors": {},
    })


async def _proxy_ws(request: web.Request, target: str,
                    session: aiohttp.ClientSession) -> web.WebSocketResponse:
    """Bidirectional WebSocket relay — ComfyUI streams execution progress over /ws, so without
    this the UI would connect to the gate and never see its own render happen."""
    client_ws = web.WebSocketResponse()
    await client_ws.prepare(request)
    async with session.ws_connect(target, headers={
            k: v for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() not in ("host", "sec-websocket-key",
                                                                 "sec-websocket-version",
                                                                 "sec-websocket-extensions")
    }) as upstream_ws:

        async def pump(src: Any, dst: Any) -> None:
            async for msg in src:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

        done, pending = await asyncio.wait(
            [asyncio.create_task(pump(client_ws, upstream_ws)),
             asyncio.create_task(pump(upstream_ws, client_ws))],
            return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    return client_ws


SESSION: web.AppKey[aiohttp.ClientSession] = web.AppKey("session")
RESIDENCY: web.AppKey[Residency] = web.AppKey("residency")
WATCHER: web.AppKey[asyncio.Task[None]] = web.AppKey("watcher")


def make_app(cfg: Config) -> web.Application:
    app = web.Application(client_max_size=0)  # 0 = unlimited; ComfyUI accepts large uploads

    async def on_startup(app: web.Application) -> None:
        session = aiohttp.ClientSession(auto_decompress=False)
        app[SESSION] = session
        residency = Residency(cfg, session)
        app[RESIDENCY] = residency
        # Reclaim anything a previous incarnation of this gate stranded, before serving traffic.
        await residency.clear_stranded()
        app[WATCHER] = asyncio.create_task(residency.watch())

    async def on_cleanup(app: web.Application) -> None:
        app[WATCHER].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app[WATCHER]
        # Give the card back on a clean shutdown instead of making the TTL sweep wait it out.
        await app[RESIDENCY].release(reason="gate shutting down")
        await app[SESSION].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def status(request: web.Request) -> web.Response:
        return web.json_response(request.app[RESIDENCY].status())

    async def handler(request: web.Request) -> web.StreamResponse:
        residency = request.app[RESIDENCY]
        session = request.app[SESSION]
        target = cfg.upstream + request.rel_url.raw_path
        if request.rel_url.raw_query_string:
            target += "?" + request.rel_url.raw_query_string

        # THE GATE. Residency is taken BEFORE the submission is forwarded, so the upstream never
        # begins GPU work on a card it has not been granted. Everything else is a plain proxy.
        if _is_submit(cfg, request):
            try:
                await residency.acquire(reason=f"{request.method} {request.path}")
            except LeaseDenied as e:
                LOG.warning("REFUSED %s %s: %s", request.method, request.path, e.message)
                return _denied_response(e)

        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _proxy_ws(request, target.replace("http", "ws", 1), session)

        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() != "host"}
        try:
            async with session.request(
                request.method, target, headers=headers,
                data=request.content if request.can_read_body else None,
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
            ) as upstream:
                out = web.StreamResponse(status=upstream.status, headers={
                    k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP})
                await out.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await out.write(chunk)
                await out.write_eof()
                return out
        except (TimeoutError, aiohttp.ClientError, OSError) as e:
            LOG.warning("upstream %s %s failed: %s", request.method, request.path, e)
            return web.json_response(status=502, data={
                "error": {"type": "upstream_unreachable",
                          "message": f"Upstream {cfg.upstream} could not be reached.",
                          "details": str(e), "extra_info": {}},
                "node_errors": {}})

    app.router.add_get(f"{CONTROL_PREFIX}/health", health)
    app.router.add_get(f"{CONTROL_PREFIX}/status", status)
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


def main() -> int:
    logging.basicConfig(level=os.environ.get("GATE_LOG_LEVEL", "INFO"),
                        format="[gpu-gate] %(levelname)s %(message)s", stream=sys.stderr)
    cfg = Config()
    problems = cfg.validate()
    if problems:
        for p in problems:
            LOG.error("config: %s", p)
        return 2
    LOG.info("gating %s on :%d — submit %s %s, queue %s, %.1fGB as %r",
             cfg.upstream, cfg.listen_port, sorted(cfg.submit_methods),
             list(cfg.submit_paths), cfg.queue_path, cfg.vram_gb, cfg.job_id)
    web.run_app(make_app(cfg), port=cfg.listen_port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
