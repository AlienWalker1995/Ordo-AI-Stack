# syntax=docker/dockerfile:1.6
# Multi-stage build for Hermes Agent.
# Stage 1: node builds the web UI bundle.
# Stage 2: python runtime with hermes installed; copies web_dist from stage 1.

# Upstream pin. ALWAYS record which release the SHA is — a bare 40-hex string tells a
# future reader nothing, and this pin silently rotted 2.5 months (see below).
#   v2026.8.3 (v0.20.0, 2026-08-03) — commit 3c27eb62, the tag's dereferenced commit.
#   NOTE: `git/ref/tags/<tag>` returns the ANNOTATED TAG object here, not the commit;
#   deref via `git/tags/<sha>` or the checkout below resolves to the wrong object.
# Previous pin 5fdcfd85 was a 2026-05-19 commit left 11 releases behind, several citing
# security fixes. It went unnoticed because the daily stack audit grades locally-built
# images as REBUILD and never looked upstream; scripts/stack_monitor.py now audits this
# very ARG (see its `pinned_sources` block), so bumping this line is what clears the
# SECURITY row it emits. Keep the `<X>_PINNED_SHA` / `<X>_REPO` naming — discovery is
# by that convention, not a hardcoded table.
ARG HERMES_PINNED_SHA=3c27eb6234bf91b8ceee9e9071591b31e9b148cb
ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent.git

# ── Stage 1: web-builder ──
# Upstream's ROOT package.json (hermes-agent@1.0.0) declares:
#   engines: {"node": ">=22.22.0", "npm": "<11.10.0 || >=11.17.0"}
# node:20-slim (node 20.20.2 / npm 10.8.2) fails `npm install` outright with EBADENGINE
# at the v2026.8.3 pin. Note the npm range EXCLUDES 11.10.0–11.16.x, so "newest npm" is
# not automatically valid — node:26-slim ships npm 11.18.0, which clears it.
# Checking web/package.json is NOT sufficient; it declares no engines and the constraint
# lives in the workspace root.
# This stage only produces the static web bundle (the runtime stage is python:3.11-slim),
# so the major bump carries no runtime risk. Digest-pinned, not a rolling tag, per the
# stack's pin-don't-float rule — bump tag and digest together.
FROM node:26-slim@sha256:80f12a4030a00d8f78ebc4602bc3ef0f984932f498649b8ed3c0a740a6dff4a8 AS web-builder
ARG HERMES_REPO
ARG HERMES_PINNED_SHA

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone --no-single-branch "$HERMES_REPO" hermes-agent \
    && cd hermes-agent \
    && git checkout "$HERMES_PINNED_SHA"

WORKDIR /build/hermes-agent/web

# No SPA base rewrite. The stack serves Hermes at its ORIGIN ROOT on the dedicated
# edge port :8447 (auth/caddy/Caddyfile — the port-per-service model), so the upstream
# root-absolute asset URLs (/assets, /ds-assets, /api, the WS endpoints) resolve as-is.
# Any future subpath deployment (X-Forwarded-Prefix) is handled at RUNTIME by upstream
# web_server.py's mount_spa, which rewrites /assets, /ds-assets, /fonts and /favicon in
# index.html + CSS to honour the prefix. A build-time `base: '/hermes/'` (the old
# spa-patches.mjs, removed) baked a prefix the port model never strips → the browser
# requested /hermes/assets/*.js, hit the SPA catch-all, got index.html, and rendered a
# blank page. Building at the upstream root base is the fix.
RUN npm install && npm run build
# Produces: /build/hermes-agent/hermes_cli/web_dist/

# ── Stage 2: runtime ──
FROM python:3.11-slim AS runtime
ARG HERMES_REPO
ARG HERMES_PINNED_SHA

# libopus0 + ffmpeg: Discord VOICE. discord.py voice needs the Opus codec to encode/decode
# voice frames and ffmpeg for audio transcode; without libopus the gateway logs
# "Opus codec not found — voice channel playback disabled" and voice mode is dead.
# Upstream's own checklist is scripts/discord-voice-doctor.py — run it in the container
# after any change here; it must report Opus + ffmpeg green.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gosu libopus0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv — fast Python package manager.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

# Docker CLI + compose plugin — lets Hermes run `docker exec`, `docker ps`,
# `docker compose ...` against the host daemon (via the mounted /var/run/docker.sock).
# Download static binaries (docker.io apt package doesn't always ship the client).
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-26.1.4.tgz" \
      -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64" \
       -o /usr/local/bin/docker-compose \
    && chmod +x /usr/local/bin/docker /usr/local/bin/docker-compose

# Non-root user (uid 1000 matches other stack services).
RUN useradd -m -u 1000 -s /bin/bash hermes

# Clone hermes-agent source into /opt, owned by hermes user.
RUN git clone --no-single-branch "$HERMES_REPO" /opt/hermes-agent \
    && cd /opt/hermes-agent \
    && git checkout "$HERMES_PINNED_SHA" \
    && chown -R hermes:hermes /opt/hermes-agent

USER hermes
WORKDIR /opt/hermes-agent

# Install Hermes + all extras into a venv at /opt/hermes-agent/.venv/.
RUN uv venv --python 3.11 \
    && uv pip install -e ".[all]"

# REMOVED 2026-08-05: the journal_mode=WAL -> DELETE sed. Do not reinstate without
# reading this — it is now actively harmful, for two independent reasons.
#
# 1. Its premise is gone. It existed because WAL needs mmap/shared-memory that is
#    unreliable on Docker Desktop Windows bind mounts (9p/virtiofs). Hermes' brain
#    is no longer a bind mount — state.db, kanban.db and sessions.db all live in the
#    `hermes-home` NAMED VOLUME (ext4 in the WSL2 VM), where WAL works normally and
#    is faster. The only SQLite files left on the 9p bind (dashboard orchestration.db,
#    open-webui webui.db) belong to other services and were never in this sed's path.
#
# 2. It corrupted upstream's own handling. v2026.8.3 added
#    hermes_state.apply_wal_with_fallback(), which probes WAL and degrades to DELETE
#    on filesystems that refuse it — including the silent-refusal case. A blunt sed
#    rewrites the pragma INSIDE that probe, so the function executes
#    `PRAGMA journal_mode=DELETE`, observes mode != "wal", and takes its
#    "WAL refused without raising" branch: a false fallback WARNING on every open, and
#    a raised WalUnsupportedError anywhere `require_wal=True` (no such call sites
#    today — which is the only reason this degraded quietly instead of crashing).
#    It also rewrote comments and docstrings, leaving upstream source claiming
#    "PRAGMA journal_mode=DELETE raises ..." where it meant WAL.
#
# Upstream's detection is strictly better than the sed: correct on ext4, correct on a
# WAL-hostile filesystem, and it cannot rot the way an anchor-matching patch does.

# Ordo AI Stack patch: allow 3D/maker file types as native outbound attachments.
# Upstream's outbound media allowlists (gateway/platforms/base.py) omit
# STL/3MF/OBJ/STEP/gcode/etc., so a generated .stl degrades to a plain-text
# link instead of a real Discord/Telegram file attachment — breaking
# "send me an STL" workflows. Two lists need it: the explicit MEDIA: tag regex
# and the bare-path auto-detect tuple (_LOCAL_MEDIA_EXTS). Non-image/video
# types already route to send_document, so once recognized they attach.
# The asserts fail the build loudly if upstream moves either anchor.
RUN python3 - <<'PYEOF'
import pathlib
b = pathlib.Path("/opt/hermes-agent/gateway/platforms/base.py")
s = b.read_text()

# v2026.8.3 consolidated what used to be TWO hand-maintained allowlists (a regex
# alternation + _LOCAL_MEDIA_EXTS) into ONE tuple, MEDIA_DELIVERY_EXTS, from which
# both are now derived. So this patch has exactly one insertion point — but it is
# only correct while that consolidation holds. Assert it, or a future upstream
# split silently half-patches us (regex updated, path detection not).
assert "MEDIA_DELIVERY_EXTS" in s, "hermes: MEDIA_DELIVERY_EXTS gone — allowlist restructured"
assert "_LOCAL_MEDIA_EXTS = MEDIA_DELIVERY_EXTS" in s, \
    "hermes: _LOCAL_MEDIA_EXTS no longer aliases MEDIA_DELIVERY_EXTS — needs a second patch site"
assert "sorted((e.lstrip(\".\") for e in MEDIA_DELIVERY_EXTS)" in s, \
    "hermes: MEDIA: regex alternation no longer derived from MEDIA_DELIVERY_EXTS"

if '".stl"' in s:
    print("outbound media allowlist already carries 3D/maker formats — nothing to do")
else:
    # Anchor on comment + closing paren: the bare '".html", ".htm",' line also occurs
    # in _TEXT_INJECT_EXTENSIONS (followed by ".css"), and patching THAT would make
    # Hermes inline 3D files as text instead of attaching them.
    anchor = '    # Web / rendered output\n    ".html", ".htm",\n)'
    assert s.count(anchor) == 1, "hermes: MEDIA_DELIVERY_EXTS tail anchor missing or ambiguous"
    exts = ['.stl', '.3mf', '.obj', '.step', '.stp', '.gcode', '.ply', '.glb', '.gltf']
    s = s.replace(anchor,
                  '    # Web / rendered output\n    ".html", ".htm",\n'
                  '    # Ordo AI Stack: 3D / maker formats\n    '
                  + ", ".join('"%s"' % e for e in exts) + ',\n)')
    b.write_text(s)
    print("patched MEDIA_DELIVERY_EXTS with 3D/maker formats")

assert '".stl"' in b.read_text(), "hermes: 3D ext patch did not stick"
PYEOF

# Ordo AI Stack patch: Discord voice AUTO-JOIN. Upstream v0.20.0 ships full-duplex voice
# but its only activation is the /voice join slash command; on_voice_state_update merely
# LOGS joins and early-returns when the bot is not connected. Operator wants "if I am in
# the voice channel it should work": when an allowlisted user is in a voice channel, the
# bot joins it (and follows channel switches). Gated by HERMES_DISCORD_VOICE_AUTO_JOIN so
# unset env = exact upstream behavior. The existing inactivity timeout still handles leave.
# Anchored on the handler's unique docstring; asserts fail the BUILD loudly if upstream
# moves it (the 2026-08-05 media-patch rot lesson: never patch blind).
RUN python3 - <<'PYEOF'
import pathlib
b = pathlib.Path("/opt/hermes-agent/plugins/platforms/discord/adapter.py")
s = b.read_text()
anchor = '''            async def on_voice_state_update(member, before, after):
                """Track voice channel join/leave events."""'''
assert s.count(anchor) == 1, "hermes voice: on_voice_state_update anchor moved/ambiguous"
assert "async def join_voice_channel" in s, "hermes voice: join_voice_channel gone — auto-join patch needs a new call path"
assert "import os" in s, "hermes voice: adapter no longer imports os"
inject = anchor + '''
                # Ordo AI Stack: auto-join the allowlisted user's voice channel.
                if os.environ.get("HERMES_DISCORD_VOICE_AUTO_JOIN", "").strip().lower() in ("1", "true", "yes"):
                    try:
                        _allowed = set()
                        for _tok in os.environ.get("DISCORD_ALLOWED_USERS", "").split(","):
                            _digits = "".join(ch for ch in _tok if ch.isdigit())
                            if len(_digits) >= 15:
                                _allowed.add(int(_digits))
                        _vc = adapter_self._voice_clients.get(member.guild.id)
                        if (member.id in _allowed and after.channel is not None
                                and member != adapter_self._client.user
                                and (_vc is None or getattr(_vc, "channel", None) != after.channel)):
                            logger.info("Voice auto-join: following %s into %s", member.display_name, after.channel.name)
                            await adapter_self.join_voice_channel(after.channel)
                    except Exception:
                        logger.exception("Voice auto-join failed")'''
s = s.replace(anchor, inject, 1)
b.write_text(s)
assert "Voice auto-join: following" in b.read_text(), "hermes voice: auto-join patch did not stick"
print("patched Discord adapter with voice auto-join (env-gated)")
PYEOF

# Web UI — copy built SPA from stage 1.
COPY --from=web-builder --chown=hermes:hermes \
    /build/hermes-agent/hermes_cli/web_dist/ \
    /opt/hermes-agent/hermes_cli/web_dist/

USER root
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Ordo AI Stack: ship the push-through plugin (bundled location, opt-in via entrypoint
# on first run) and the seeded SOUL.md. See docs/superpowers/specs/2026-04-21-hermes-push-through-design.md.
COPY --chown=hermes:hermes plugins/push-through/ /opt/hermes-agent/plugins/push-through/
COPY --chown=hermes:hermes seed/SOUL.md /opt/ordo-seed/SOUL.md

# Ordo AI Stack: ship the ops-router plugin alongside push-through. Exposes
# ops-controller verbs (list_containers / container_logs / restart_container)
# as first-class tools so the model never tries `terminal: docker ...` (which
# fails — Plan C removed the docker.sock mount). Co-locate the canonical
# ops_client.py inside the plugin dir so the plugin can `from .ops_client import OpsClient`.
COPY --chown=hermes:hermes plugins/ops-router/ /opt/hermes-agent/plugins/ops-router/
COPY --chown=hermes:hermes ops_client.py /opt/hermes-agent/plugins/ops-router/ops_client.py

# Stay as root through the final stage so the entrypoint can chmod the
# bind-mounted $HERMES_HOME (host UID and container hermes UID often diverge
# on Docker Desktop / virtiofs). Entrypoint drops to hermes via gosu before
# exec'ing the CMD — same pattern as dashboard/entrypoint.sh.
USER root
ENV HERMES_HOME=/home/hermes/.hermes
ENV PYTHONIOENCODING=utf-8
ENV PATH=/opt/hermes-agent/.venv/bin:$PATH

WORKDIR /workspace

ENTRYPOINT ["/entrypoint.sh"]
CMD ["hermes", "--help"]
