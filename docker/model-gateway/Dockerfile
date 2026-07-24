# Pin to specific version for reproducible builds (floating :main-stable can break silently)
FROM ghcr.io/berriai/litellm:main-stable

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
