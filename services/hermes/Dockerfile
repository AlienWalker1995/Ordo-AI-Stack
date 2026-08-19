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

# ── Stage 1b: sqlite-builder ──
# hermes_state refuses to ENABLE WAL when the linked SQLite has the WAL-reset corruption
# bug (3.7.0–3.51.2, sqlite.org/wal.html#walresetbug) and falls back to
# journal_mode=DELETE for every brain DB (state.db ~1.3GB incl. trigram FTS, kanban.db,
# cron/executions.db). DELETE mode does full-journal fsync per commit; under context-
# compression write bursts that wedged the gateway event loop inside os.fsync long enough
# to trip the shutdown watchdog (exit 75) — the near-daily unclean exits in
# gateway-exit-diag.log (root-caused 2026-08-08). Debian trixie ships libsqlite3 3.46.1
# and will never bump to 3.51.3+, and `hermes update` cannot replace an OS library, so we
# build a fixed SQLite and let /usr/local/lib win soname resolution (the stdlib _sqlite3
# extension links libsqlite3.so.0 dynamically; Debian's ld.so searches /usr/local/lib
# before the multiarch dir). --all enables FTS5 (messages_fts + trigram) and the other
# loadable features Debian's build carried; the runtime stage asserts both version and
# trigram FTS5 at build time so a flag regression fails the build, not the gateway.
FROM python:3.11-slim AS sqlite-builder
ARG SQLITE_YEAR=2026
ARG SQLITE_AUTOCONF=3530400
# sha256 of sqlite-autoconf-3530400.tar.gz (3.53.4), computed 2026-08-08.
ARG SQLITE_SHA256=0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c
# libc6-dev is only a Recommends of gcc — without it there is "no working C compiler".
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc make libc6-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL "https://sqlite.org/${SQLITE_YEAR}/sqlite-autoconf-${SQLITE_AUTOCONF}.tar.gz" \
      -o /tmp/sqlite.tar.gz \
    && echo "${SQLITE_SHA256}  /tmp/sqlite.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/sqlite.tar.gz -C /tmp \
    && cd "/tmp/sqlite-autoconf-${SQLITE_AUTOCONF}" \
    && CFLAGS="-O2 -DSQLITE_ENABLE_COLUMN_METADATA=1 -DSQLITE_ENABLE_UNLOCK_NOTIFY=1 -DSQLITE_ENABLE_DBSTAT_VTAB=1 -DSQLITE_MAX_VARIABLE_NUMBER=250000" \
       ./configure --prefix=/usr/local --disable-static --all \
    && make -j"$(nproc)" && make install

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

# Fixed SQLite (see sqlite-builder stage). Copy the shared lib, refresh the loader
# cache, and prove at BUILD time that (a) the stdlib picked it up, (b) it is past the
# WAL-reset bug, and (c) FTS5+trigram — which state.db's messages_fts requires — work.
COPY --from=sqlite-builder /usr/local/lib/libsqlite3.so* /usr/local/lib/
RUN ldconfig \
    && python3 - <<'SQLEOF'
import sqlite3
v = sqlite3.sqlite_version_info
assert v >= (3, 51, 3), f"linked SQLite {sqlite3.sqlite_version} still WAL-reset vulnerable"
conn = sqlite3.connect(":memory:")
conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
conn.execute("INSERT INTO t VALUES ('hello world')")
assert conn.execute("SELECT count(*) FROM t WHERE t MATCH 'ello'").fetchone()[0] == 1
print("sqlite", sqlite3.sqlite_version, "with fts5+trigram: OK")
SQLEOF

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

# Non-root user (uid 1000 matches other stack services). Add hermes to the root group (gid 0)
# so the gosu-dropped gateway can read the root:root /var/run/docker.sock it mounts. Compose
# `group_add: ["0"]` alone is NOT enough: the entrypoint drops privileges with `gosu hermes`,
# which recomputes supplementary groups from /etc/group (where hermes must actually be a member)
# and discards the compose group_add. This is the layer that makes docker work for the real
# gateway process, not just `docker exec -u hermes`.
RUN useradd -m -u 1000 -s /bin/bash hermes \
    && usermod -aG root hermes

# Clone hermes-agent source into /opt, owned by hermes user.
RUN git clone --no-single-branch "$HERMES_REPO" /opt/hermes-agent \
    && cd /opt/hermes-agent \
    && git checkout "$HERMES_PINNED_SHA" \
    && chown -R hermes:hermes /opt/hermes-agent

# PATCH: pressure-pass condensation of oversized USER messages (pass 5).
# A giant user paste (e.g. an exported transcript handed to Hermes as the task)
# deadlocked compression on 2026-08-15: after compaction the whole transcript sat
# inside the protected tail, every demotion pass spares user rows, so preflight
# ended in no_progress forever while the request could only grow past the model
# window (llamacpp rejected 131,147 > 131,072 tokens; 10 doomed 30-min retries).
# The patch lets the #61932 pressure pass — as its FINAL escalation — condense a
# non-active oversized user message to a head/tail excerpt with an explicit
# marker (full original stays in the session store). Includes upstream-style
# tests (tests/agent/test_context_compressor.py — 113 pass at this pin).
# --check first: the build fails loudly if a future pin bump breaks the patch.
COPY pressure-user-condense.diff /tmp/pressure-user-condense.diff
RUN cd /opt/hermes-agent \
    && git apply --recount --check /tmp/pressure-user-condense.diff \
    && git apply --recount /tmp/pressure-user-condense.diff \
    && chown hermes:hermes agent/context_compressor.py tests/agent/test_context_compressor.py \
    && echo "pressure-user-condense patch applied"

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

# Ordo AI Stack patch (DIAGNOSTIC ONLY): name the caller on every voice-channel leave.
#
# An AUTO-JOIN patch briefly lived here (2026-08-05, three iterations) and was REMOVED
# the same day — do not reintroduce it. Lessons, paid for live:
#   * Upstream has NO auto-join; it is open idea #33683. The web claims of a
#     `discord.voice.auto_join` config are extrapolated from that issue — verify with a
#     code search (`auto_join_channels` -> 0 hits), not docs summaries.
#   * Every mystery that looked like "the adapter double-fires events" — duplicate
#     threads per message, doubled voice_state logs, "Already connected" zombies, an
#     unexplained voice disconnect — was actually a LEAKED SECOND GATEWAY: a
#     `docker compose run --rm agent` debug container survives when the compose client
#     is timeout-killed, and two processes then share one bot token and one brain.
#     Before debugging "impossible" duplicate behavior: `docker ps --filter
#     name=ordo-agent-run`. Never leave a foreground gateway run unreaped.
#   * Even so, patching event handlers we don't own remains the wrong seam — the
#     adapter's voice lifecycle (locks, receiver, mixer) is not a public surface.
#   * The SUPPORTED equivalent of "if I'm in the channel it works": run /voice join once
#     and set `discord.voice_channel_inactivity_timeout_seconds: 0` in config.yaml
#     (minimum=0 means 0 disables the auto-leave) — the bot stays resident in the VC.
# What remains below is one INFO line instrumenting leave_voice_channel, because a leave
# fired 2026-08-05 23:43 with NO recorded initiator (provably not the inactivity timeout).
# Remove once upstream logs its own leave reasons.
RUN python3 - <<'PYEOF'
import pathlib
b = pathlib.Path("/opt/hermes-agent/plugins/platforms/discord/adapter.py")
s = b.read_text()

# Instrument leave_voice_channel with its caller. Something tore the bot out of the
# channel ~30s after TTS on 2026-08-05 (not the inactivity timeout — 300s, and its
# text notification never fired) and INFO logs recorded no initiator. One line names
# the caller on every future leave instead of leaving us to guess again.
anchor2 = '''    async def leave_voice_channel(self, guild_id: int) -> None:
        """Disconnect from the voice channel in a guild."""'''
assert s.count(anchor2) == 1, "hermes voice: leave_voice_channel anchor moved/ambiguous"
inject2 = anchor2 + '''
        import traceback as _tb
        logger.info("leave_voice_channel(guild=%s) called by:\\n%s", guild_id,
                    "".join(_tb.format_stack(limit=6)[:-1]))'''
s = s.replace(anchor2, inject2, 1)

b.write_text(s)
assert "leave_voice_channel(guild=%s) called by" in b.read_text(), "hermes voice: leave instrumentation did not stick"
print("patched Discord adapter with leave instrumentation")
PYEOF

# Ordo AI Stack patch: /voice join must PRESERVE an existing per-channel voice mode.
#
# Upstream's join handler unconditionally sets the bound text channel's mode to "all"
# (speak EVERY reply, typed text included) and persists it — clobbering an operator's
# saved "voice_only" ("voice in -> voice out; text in -> text out"). The only recovery
# is remembering to run /voice on after EVERY rejoin; forgetting it silently degrades
# to the bot talking over the text channel. Unlike the removed auto-join patch (a
# missing surface), this is a one-line bug on the SUPPORTED tri-state surface —
# upstream reaffirmed the voice_mode design when closing PR #19521, and their meta
# issue #78207 lane 02 tracks exactly this decision layer. setdefault keeps upstream's
# "all" for channels with no saved mode (non-breaking) and respects a saved one.
# Drop this block when an upstream fix lands.
RUN python3 - <<'PYEOF'
import pathlib
b = pathlib.Path("/opt/hermes-agent/gateway/run.py")
s = b.read_text()

anchor = '''            self._voice_mode[self._voice_key(event.source.platform, event.source.chat_id)] = "all"
            self._save_voice_modes()'''
assert s.count(anchor) == 1, "hermes voice: join-handler mode-set anchor moved/ambiguous"
s = s.replace(anchor, '''            _vkey = self._voice_key(event.source.platform, event.source.chat_id)
            self._voice_mode.setdefault(_vkey, "all")
            self._save_voice_modes()''', 1)

# The canned reply hard-codes "I'll speak my replies" — false when a preserved mode
# is voice_only/off. Reflect the effective mode instead.
reply_anchor = '''            return (
                f"Joined voice channel **{voice_channel.name}**.\\n"
                f"I'll speak my replies and listen to you. Use /voice leave to disconnect."
            )'''
assert s.count(reply_anchor) == 1, "hermes voice: join-handler reply anchor moved/ambiguous"
s = s.replace(reply_anchor, '''            _vmode = self._voice_mode.get(_vkey, "all")
            _speak = ("I'll speak my replies and listen to you."
                      if _vmode == "all"
                      else f"I'll listen; replies follow your saved voice mode ({_vmode}).")
            return (
                f"Joined voice channel **{voice_channel.name}**.\\n"
                f"{_speak} Use /voice leave to disconnect."
            )''', 1)

b.write_text(s)
assert 'self._voice_mode.setdefault(_vkey, "all")' in b.read_text(), "hermes voice: join mode-preserve patch did not stick"
print("patched gateway: /voice join preserves saved voice mode")
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

# Vault federation: two-way sync of SOUL/memories with the Obsidian vault
# (spec: docs/superpowers/specs/2026-08-19-memory-stack-overhaul-design.md).
COPY --chown=hermes:hermes scripts/vault_federate.py /opt/ordo/vault_federate.py

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
