# ops-api — the V1-PARITY dashboard BACKEND, running inside the ordo stack.
#
# This is the feature-rich FastAPI control API the original (V1) dashboard talks to:
# /model-config (flag cards), /registry/* (model↔GPU registry), /guardian/* (status),
# /services/*, /gpu/*, /mcp/containers, /audit, /models/* (GGUF mgmt helpers).
#
# It is BUILT FROM A COPY of C:/dev/ordo-ai-stack/ops-controller with ONE minimal,
# clearly-commented patch (see main.py "V2 PATCH"): an OPS_COMPOSE_MUTATIONS_ENABLED
# kill-switch (default OFF) that turns the compose-mutation endpoints into a static
# 501 so this backend can NEVER shell `docker-compose up/down` against the stack
# (the 2026-06-26 secret-less-recreate outage shape). The reactive GUARDIAN needs no
# code patch — it is opt-in via COMFYUI_SERIALIZE_LLAMACPP (default "0"), so leaving
# that env unset (which the ops-api service does) means the guardian thread never
# starts. The V2 `ordo serve` scheduler (service `ops-controller`) remains the sole
# GPU/compose authority.
#
# Build:  docker build -t ordo/ops-api:latest docker/ops-api
FROM python:3.12-slim

WORKDIR /app

# Install docker compose v2 CLI (SDK start/stop use the socket; compose CLI is only
# reachable when OPS_COMPOSE_MUTATIONS_ENABLED=1, which this deployment never sets).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64" \
       -o /usr/local/bin/docker-compose \
    && chmod +x /usr/local/bin/docker-compose \
    && apt-mark manual ca-certificates \
    && apt-get purge -y --auto-remove curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py audit.py model_registry.py gpu_assignments_fmt.py llamacpp_flags.py compose_recreate.py ./

# Run as non-root user (docker group for socket access; Docker Desktop's socket is
# root:root, so the ops-api service also uses group_add: ["0"] in compose).
RUN groupadd -g 999 docker && useradd -m -u 1000 -G docker appuser
USER appuser

EXPOSE 9000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
