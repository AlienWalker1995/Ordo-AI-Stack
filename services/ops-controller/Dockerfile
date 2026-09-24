# ops-controller — the Ordo control plane (ordo serve).
# Build context is the REPO ROOT (the root .dockerignore allowlists only the render-data dirs,
# so the context stays tiny):  docker build -f services/ops-controller/Dockerfile -t ordo/ops-controller:latest .
# Renders config, exposes /status + /model-config + /jobs, and drives the GPU broker.
# It needs the Docker CLI to start/stop containers, but the broker's DockerBackend
# guard scopes every action to the ordo project prefix, so it can never touch anything else.
# python:3.11-slim
FROM python@sha256:6d85378d88a19cd4d76079817532d62232be95757cb45945a99fec8e8084b9c2

# Static Docker client only (no daemon) — the broker shells out to `docker start/stop`.
# Pinned by tag and digest to the host's Docker (29.2.1, which bundles compose v5.1.0). The two
# compose versions must match: they normalise bind mounts differently, so a container created by
# one side reads as changed to the other and recreates cascade. Bump this with the host's Docker.
COPY --from=docker:29.2.1-cli@sha256:cab69e2d0a1a2ea9a1ce1060252f439e83483ae41ec09317aecb33b08a0656a5 /usr/local/bin/docker /usr/local/bin/docker
# ...and the compose PLUGIN, because `docker compose` is a plugin, not a subcommand of that binary.
# Without it every route built on DockerBackend._compose() (compose up/down/restart, service
# recreate) answers 500 with docker's own help text, while mock-backed tests stay
# green — the same shape as the slice-1 protocol gap. Guarded by
# tests/substrate/test_ops_controller_image.py.
COPY --from=docker:29.2.1-cli@sha256:cab69e2d0a1a2ea9a1ce1060252f439e83483ae41ec09317aecb33b08a0656a5 /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /app
COPY services/ops-controller/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# The substrate package + the co-located render data (the source ordo.yaml is mounted at /config,
# so a runtime model switch re-renders in place — one write path). services/ holds EVERY render
# manifest (plugin.yaml / agent.yaml / dashboard.yaml under services/<id>/): the agent and
# dashboard manifests are as load-bearing as the plugin manifests — without services/ a render
# inside this image silently regresses the compose to the native dashboard + bare agent (live
# incident 2026-07-15). Guarded by
# tests/substrate/test_parity_render.py::test_ops_controller_image_ships_all_render_data.
COPY ordo ./ordo
COPY catalog ./catalog
COPY services ./services

ENV PYTHONUNBUFFERED=1
EXPOSE 9000
ENTRYPOINT ["python", "-m", "ordo.cli"]
# --source/--catalog are global (pre-subcommand) flags; --project/--out belong to `serve`.
CMD ["--source", "/config/ordo.yaml", "serve", "--project", "ordo", "--out", "/config"]
