# syntax=docker/dockerfile:1.6
# Multi-stage build for Hermes Agent.
# Stage 1: node builds the web UI bundle.
# Stage 2: python runtime with hermes installed; copies web_dist from stage 1.

ARG HERMES_PINNED_SHA=73bf3ab1b22314ed9dfecbb59242c03742fe72af
ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent.git

# ── Stage 1: web-builder ──
FROM node:20-slim AS web-builder
ARG HERMES_REPO
ARG HERMES_PINNED_SHA

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone --no-single-branch "$HERMES_REPO" hermes-agent \
    && cd hermes-agent \
    && git checkout "$HERMES_PINNED_SHA"

WORKDIR /build/hermes-agent/web
RUN npm install && npm run build
# Produces: /build/hermes-agent/hermes_cli/web_dist/

# ── Stage 2: runtime ──
FROM python:3.11-slim AS runtime
ARG HERMES_REPO
ARG HERMES_PINNED_SHA

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl \
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

# Ordo AI Stack patch: replace SQLite journal_mode=WAL with DELETE across all
# Hermes DB handles. WAL mode requires mmap/shared-memory features that don't
# work reliably on Docker Desktop Windows bind mounts (9p/virtiofs filesystem).
# DELETE mode is slower but works on any POSIX-ish filesystem.
RUN find /opt/hermes-agent -type f -name "*.py" -not -path "*/tests/*" -not -path "*/.venv/*" \
    -exec sed -i 's/PRAGMA journal_mode=WAL/PRAGMA journal_mode=DELETE/g' {} +

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

USER hermes
ENV HERMES_HOME=/home/hermes/.hermes
ENV PYTHONIOENCODING=utf-8
ENV PATH=/opt/hermes-agent/.venv/bin:$PATH

WORKDIR /workspace

ENTRYPOINT ["/entrypoint.sh"]
CMD ["hermes", "--help"]
