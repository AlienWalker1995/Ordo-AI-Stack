# ops-controller — the v2 control plane (ordo serve).
# Build context is v2/.  Renders config, exposes /status + /model-config + /jobs, and drives the
# GPU broker.  It needs the Docker CLI to start/stop containers, but the broker's DockerBackend
# guard scopes every action to the ordo project prefix, so it can never touch the live stack.
FROM python:3.11-slim

# Static Docker client only (no daemon) — the broker shells out to `docker start/stop`.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
RUN pip install --no-cache-dir pyyaml

# The substrate package + EVERY render data dir (the source ordo.yaml is mounted at /config,
# so a runtime model switch re-renders in place — one write path). agents/ and dashboards/ are
# as load-bearing as plugins/: without them a render inside this image silently regresses the
# compose to the native dashboard + bare agent (live incident 2026-07-15). Guarded by
# v2/tests/test_parity_render.py::test_ops_controller_image_ships_all_render_data.
COPY ordo ./ordo
COPY catalog ./catalog
COPY plugins ./plugins
COPY agents ./agents
COPY dashboards ./dashboards

ENV PYTHONUNBUFFERED=1
EXPOSE 9000
ENTRYPOINT ["python", "-m", "ordo.cli"]
# --source/--catalog are global (pre-subcommand) flags; --project/--out belong to `serve`.
CMD ["--source", "/config/ordo.yaml", "serve", "--project", "ordo", "--out", "/config"]
