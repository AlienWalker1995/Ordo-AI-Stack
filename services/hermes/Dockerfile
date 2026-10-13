# syntax=docker/dockerfile:1.6
# Multi-stage build for Hermes Agent.
# Stage 1: node builds the web UI bundle.
# Stage 2: python runtime with hermes installed; copies web_dist from stage 1.

# Upstream pin. ALWAYS record which release the SHA is — a bare 40-hex string tells a
# future reader nothing, and this pin silently rotted 2.5 months (see below).
#   v0.21.7 (2026-10-11) — commit 71d34bad, the tag's dereferenced commit.
#   The image runs this commit UNMODIFIED: no diffs, no anchor patches. Stack behavior is
#   configuration (services/hermes/entrypoint.sh) and repo-owned skills and plugins. A
#   Hermes behavior that config cannot express is an upstream issue, not a local patch.
#   NOTE: `git/ref/tags/<tag>` returns the ANNOTATED TAG object here, not the commit;
#   deref via `git/tags/<sha>` or the checkout below resolves to the wrong object.
# Previous pin 5fdcfd85 was a 2026-05-19 commit left 11 releases behind, several citing
# security fixes. It went unnoticed because the daily stack audit grades locally-built
# images as REBUILD and never looked upstream; scripts/stack_monitor.py now audits this
# very ARG (see its `pinned_sources` block), so bumping this line is what clears the
# SECURITY row it emits. Keep the `<X>_PINNED_SHA` / `<X>_REPO` naming — discovery is
# by that convention, not a hardcoded table.
ARG HERMES_PINNED_SHA=71d34bad1045610252eda02ee79549436896e784
ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent.git

# ── Stage 1: web-builder ──
# Upstream's ROOT package.json (hermes-agent@1.0.0) declares:
#   engines: {"node": ">=22.22.0", "npm": "<11.10.0 || >=11.17.0"}
# node:20-slim (node 20.20.2 / npm 10.8.2) fails `npm install` outright with EBADENGINE
# at the v2026.8.3 pin. Note the npm range EXCLUDES 11.10.0–11.16.x, so "newest npm" is
# not automatically valid — node:26-slim ships npm 11.18.0, which clears it.
# Checking web/package.json is NOT sufficient; it declares no engines and the constraint
# lives in the workspace root.
# This stage only produces the static web bundle (the runtime stage is python:3.14-slim),
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
FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS sqlite-builder
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
# Python 3.14: upstream supports only 3.14 since v0.21.6. pyproject declares every core dependency
# with `python_version >= '3.14'` (older interpreters are allowed only so old installs can run
# their update step), so a 3.11 venv installs without openai, ruamel.yaml or dotenv and the
# gateway cannot import. Bump with upstream's .python-version.
FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime
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

# uv (fast Python package manager), pinned via the versioned installer URL.
RUN curl -LsSf https://astral.sh/uv/0.12.3/install.sh | sh \
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

# SKILLS OWNED BY THIS REPO: upstream's optional ComfyUI skill plus this stack's deployment notes.
# The notes (skill-overlays/comfyui/ordo-deployment.md) are spliced into SKILL.md ahead of the
# upstream quick start, which otherwise tells the agent to install and launch its own ComfyUI, and
# tell it to pass --host "$COMFYUI_URL" (the GPU admission gate) to every script. Upstream's code is
# not modified: its scripts already take --host, and the stock default (127.0.0.1:8188) has nothing
# listening in this container, so a missed --host fails to connect instead of skipping the lease.
# The result goes to /opt/ordo-skills, root-owned and read-only, which the entrypoint registers
# as a Hermes `skills.external_dirs` entry: the curator will not rewrite it, the bundled sync
# defers to it, and a volume loss cannot take it. Starting from the image's own bundled copy
# means upstream fixes arrive with every hermes-agent pin bump.
COPY skill-overlays/ /tmp/skill-overlays/
COPY scripts/ordo_skills.py /opt/ordo/ordo_skills.py
RUN cd /opt/hermes-agent \
    && python3 /opt/ordo/ordo_skills.py build \
           optional-skills/creative/comfyui \
           /tmp/skill-overlays/comfyui/ordo-deployment.md \
           /opt/ordo-skills/creative/comfyui \
    && chown -R root:root /opt/ordo-skills /opt/ordo/ordo_skills.py \
    && chmod -R a+rX,go-w /opt/ordo-skills /opt/ordo/ordo_skills.py \
    && rm -rf /tmp/skill-overlays \
    && echo "ordo skills built"

# Install stamp: the version `hermes --version`, `hermes dump` and the dashboard report. Since
# v0.21.6 hermes_cli reads it from install-stamp.json (written by upstream's packagers), not from
# pyproject (0.0.0) or git, so a plain clone reports "vunknown". Same script and flags as upstream's
# Docker build; the base version and distance come from the pinned checkout's nearest release tag,
# so HERMES_PINNED_SHA stays the one source of truth. git runs as root on the hermes-owned clone,
# hence the per-command safe.directory.
RUN cd /opt/hermes-agent     && export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/opt/hermes-agent     && described="$(git describe --tags --long --match 'v[0-9]*')"     && base_tag="${described%-*-g*}"     && distance="${described%-g*}" && distance="${distance##*-}"     && python3 scripts/write_install_stamp.py --output install-stamp.json            --commit "$(git rev-parse HEAD)" --base-version "${base_tag#v}" --distance "$distance"            --source ci --distribution docker --update-mechanism external     && chown hermes:hermes install-stamp.json

USER hermes
WORKDIR /opt/hermes-agent

# Install Hermes into a venv at /opt/hermes-agent/.venv/. `all` is upstream's general bundle; it
# no longer carries any messaging platform, so the Discord gateway's own extra (discord.py[voice]:
# PyNaCl + davey for voice and DAVE E2EE) is named here. Without it Hermes lazy-installs those from
# PyPI on every fresh container start, which makes the gateway depend on the network at boot and
# the image differ from what runs.
RUN uv venv --python 3.14 \
    && uv pip install -e ".[all,discord]"

# Langfuse SDK - the optional runtime dependency of the BUNDLED `observability/langfuse`
# plugin (plugins/observability/langfuse/, shipped by upstream, enabled by entrypoint.sh
# when the langfuse plugin provides project keys). The plugin does
# `try: from langfuse import Langfuse, propagate_attributes / except: hooks are inert`,
# so without this line the plugin can be "enabled" and still trace nothing, silently -
# which is precisely the failure mode worth removing from an observability tool.
#
# Installed into the SAME venv hermes-agent itself runs from: WORKDIR is still
# /opt/hermes-agent, so `uv pip install` discovers the ./.venv created just above (the same
# discovery the `uv pip install -e` line relies on). It must not land in a second
# interpreter, or the gateway process would not see it.
#
# Pinned to 4.15.1 - the SDK major that matches the self-hosted server major (Langfuse v4,
# services/langfuse/plugin.yaml). It carries every symbol the plugin touches (Langfuse,
# propagate_attributes, create_trace_id, start_as_current_observation, start_observation,
# flush) and, unlike the 3.x line, exposes `langfuse.__version__` - which is how the
# deployment check verifies the install inside the container. Bump tag and server together.
#
# NOT the newest release, on purpose. Upstream's pyproject sets `[tool.uv] exclude-newer =
# "14 days"` - a ROLLING cutoff relative to BUILD time - so uv refuses any distribution
# published in the last fortnight and a just-released pin fails the build outright
# ("filtered by exclude-newer", seen with 4.15.2 on 2026-09-14). Versions only ever age INTO
# the window, never out of it, so a pin older than the cutoff is stable forever. When bumping,
# pick a release that is already more than 14 days old rather than adding an
# exclude-newer-package override, which would opt this one dependency out of the soak upstream
# deliberately applies to everything else.
RUN uv pip install "langfuse==4.15.1"

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

# Web UI — copy built SPA from stage 1.
COPY --from=web-builder --chown=hermes:hermes \
    /build/hermes-agent/hermes_cli/web_dist/ \
    /opt/hermes-agent/hermes_cli/web_dist/

USER root
COPY entrypoint.sh /entrypoint.sh
# git authenticates to github.com through this helper, which reads GITHUB_BACKUP_PAT from its file
# secret when asked (services/hermes/git-credential-github-pat). Nothing secret is in the config.
COPY git-credential-github-pat /usr/local/bin/git-credential-github-pat
RUN chmod 0755 /usr/local/bin/git-credential-github-pat \
    && git config --system credential.https://github.com.helper /usr/local/bin/git-credential-github-pat
RUN chmod +x /entrypoint.sh

# Ordo AI Stack: the seeded SOUL.md (first run only; the entrypoint copies it into an empty home).
COPY --chown=hermes:hermes seed/SOUL.md /opt/ordo-seed/SOUL.md

# Vault federation: two-way sync of SOUL/memories with the Obsidian vault
# (introduced in #181; behavior in scripts/vault_federate.py's docstring).
COPY --chown=hermes:hermes scripts/vault_federate.py /opt/ordo/vault_federate.py

# Ordo AI Stack: ship the ops-router plugin. Exposes
# ops-controller verbs (list_containers / container_logs / restart_container)
# as first-class tools, the audited control-plane path for Ordo services. Co-locate the canonical
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
