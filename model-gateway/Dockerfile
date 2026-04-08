FROM ghcr.io/berriai/litellm:main-stable

WORKDIR /app

COPY litellm_config.yaml /app/config.template.yaml
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 11435

ENTRYPOINT ["/app/entrypoint.sh"]
