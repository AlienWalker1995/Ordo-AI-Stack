# Pinned to the VERSION RUNNING in production (v1.82.3), by tag + digest (audit P1-4 — the
# floating :main-stable regressed once and its newer bases dropped pip, breaking this build).
# To bump: pick a specific vX.Y.Z, resolve its digest (docker manifest inspect -v), validate.
FROM ghcr.io/berriai/litellm:v1.82.3@sha256:ac95e49049e0bb5f2c5a2b0f0452e5d968844b8196cf6efbd2e77d6ef862f7e5

WORKDIR /app

# httpx is already a litellm dep, but pin a known-async version to be safe.
RUN pip install --no-cache-dir "httpx>=0.27"

COPY litellm_config.yaml /app/config.template.yaml
# Install throughput_callback into site-packages directly so LiteLLM's
# proxy-init callback importer finds it regardless of how PYTHONPATH is
# inherited (PYTHONPATH-only loading proved unreliable at startup).
COPY throughput_callback.py /usr/lib/python3.13/site-packages/throughput_callback.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 11435

ENTRYPOINT ["/app/entrypoint.sh"]
