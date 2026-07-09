# Ops Controller

Secure Docker Compose control plane. Exposes an authenticated API for start/stop/restart, logs, and image pulls. Dashboard calls this service; it never mounts docker.sock.

**Status:** See [Product Requirements Document](../docs/Product%20Requirements%20Document.md) for design and decisions.

## Endpoints

- `GET /health` — Controller health
- `GET /services` — List compose services + status
- `POST /services/{id}/start|stop|restart` — Service lifecycle (requires `confirm: true`)
- `GET /services/{id}/logs` — Tail logs
- `POST /images/pull` — Pull images for services
- `GET /audit` — Audit log

## Auth

Bearer token via `OPS_CONTROLLER_TOKEN`. Generate: `openssl rand -hex 32`.

## Security

- Never expose controller port to the public internet
- Token required for all mutating operations
- Audit log records admin actions
