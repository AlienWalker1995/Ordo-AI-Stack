# ── Stage 1: build the React SPA (frontend/dist) ────────────────────────────────
# Produces hashed ES-module assets that satisfy the backend's strict CSP
# (script-src 'self'). npm ci uses the committed lockfile for a reproducible build.
FROM node:lts-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python/FastAPI runtime ─────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gosu \
  && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app.py imports dashboard.* — copy package tree and expose it on PYTHONPATH
COPY *.py ./dashboard/
COPY templates ./dashboard/templates/
# static/ still carries the preserved legacy shell (legacy-index.html) + favicon.svg,
# reachable at /legacy-index.html as a fallback while the React port is validated.
COPY static ./dashboard/static/

# The built React SPA — FastAPI serves this at root (app.py prefers frontend/dist).
COPY --from=frontend /build/dist ./dashboard/frontend/dist/

ENV PYTHONPATH=/app

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]
