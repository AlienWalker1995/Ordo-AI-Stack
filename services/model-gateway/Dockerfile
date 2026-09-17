# Pinned to a specific LiteLLM stable by tag + index digest (there is no `-stable` suffix after
# 1.83; plain SemVer is the stable channel). To bump: pick vX.Y.Z, resolve the digest with
# `docker buildx imagetools inspect ghcr.io/berriai/litellm:vX.Y.Z`, rebuild, run Task-20-style
# acceptance (throughput callback + /mcp) before deploying.
FROM ghcr.io/berriai/litellm:v1.100.1@sha256:a3715fa7ad8387941ab697259bd2881d68931657247a41984f90fae6d11c62bf

WORKDIR /app

COPY litellm_config.yaml /app/config.template.yaml
# The callback + helpers live in /app; the entrypoint copies the callback next to the rendered
# config because LiteLLM resolves `callbacks:` module paths relative to the config file's dir.
COPY throughput_callback.py /app/throughput_callback.py
COPY merge_mcp_config.py /app/merge_mcp_config.py
COPY add_callbacks.py /app/add_callbacks.py
COPY bootstrap_keys.py /app/bootstrap_keys.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 11435

ENTRYPOINT ["/app/entrypoint.sh"]
