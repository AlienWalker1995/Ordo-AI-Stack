FROM python:3.12-slim

WORKDIR /app

# Install docker compose v2 CLI (needed for container recreation with updated env)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64" \
       -o /usr/local/bin/docker-compose \
    && chmod +x /usr/local/bin/docker-compose \
    && apt-mark manual ca-certificates \
    && apt-get purge -y --auto-remove curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py audit.py ./

# Run as non-root user (docker group for socket access)
RUN groupadd -g 999 docker && useradd -m -u 1000 -G docker appuser
USER appuser

EXPOSE 9000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
