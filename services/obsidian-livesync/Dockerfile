# ordo/livesync-bridge — headless CouchDB <-> filesystem mirror for Obsidian Self-hosted LiveSync.
#
# Built from PINNED vrtmrz/livesync-bridge source (a Deno daemon). Runs as uid 1000 so notes
# materialized into the shared vault are owned by the same uid as DATA_PATH (the memory-vault MCP
# also runs as 1000) — the image's own `deno` user is uid 1993, which would break vault perms.

# ── stage 1: fetch the pinned source ──────────────────────────────────────────
FROM alpine/git@sha256:b4d933c7185b5cc9da7704f2c6e19d477f935ea31a50d9a017ffce2bbd1109a3 AS src
ARG BRIDGE_REF=f27f78cc09b4dcfdceb952f6a222c82d51fee31f
# --recurse-submodules + submodule update: the LiveSync core lives in the `lib/` submodule, without
# which main.ts fails to resolve (file:///app/lib/src/common/logger.ts). Pin to BRIDGE_REF, then
# check the submodules out to the commits that ref records.
RUN git clone --recurse-submodules https://github.com/vrtmrz/livesync-bridge /src \
 && git -C /src checkout "${BRIDGE_REF}" \
 && git -C /src submodule update --init --recursive \
 && rm -rf /src/.git

# ── stage 2: runtime ──────────────────────────────────────────────────────────
FROM denoland/deno@sha256:2221eafd8a7556693307ad9889a79d4974e1844a6964acbe466ab9faa15d37bf
# curl: the entrypoint waits on CouchDB and creates the system/notes DBs (idempotent) before sync.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=src /src /app
COPY entrypoint.sh /usr/local/bin/livesync-bridge-entrypoint
# Resilience patch for the vendored LiveSync core: guard the per-document getByMeta() inside the
# watch try/catch so one corrupted/oversized doc can't stall the whole changes feed (see the script
# header). Self-verifying — fails the build if the upstream pattern is gone.
COPY patch-watch-resilience.ts /usr/local/lib/patch-watch-resilience.ts
# Own /app + the Deno cache dir as uid 1000 so the runtime process (config write to /app/dat,
# note writes to /app/data/notes) and the build-time cache all work as the vault-aligned uid.
RUN chmod +x /usr/local/bin/livesync-bridge-entrypoint \
 && mkdir -p /app/data /app/dat /deno-dir \
 && chown -R 1000:1000 /app /deno-dir
ENV DENO_DIR=/deno-dir
USER 1000:1000
# Apply the vendored-core resilience patch before caching, so the compiled/cached module is the
# patched one. Runs as uid 1000 (owns /app after the chown above); aborts the build on drift.
RUN deno run --allow-read --allow-write /usr/local/lib/patch-watch-resilience.ts
# `deno install` may partial-fail (optional deps) — tolerate. `deno cache` is STRICT: a missing
# module (e.g. an un-cloned submodule) must fail the build here, not at container start.
RUN deno install || true
RUN deno cache main.ts
ENTRYPOINT ["livesync-bridge-entrypoint"]
