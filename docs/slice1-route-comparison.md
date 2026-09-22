# Slice 1: Service Lifecycle — Route Comparison

This table documents the 15 service lifecycle routes ported from ops-api to ops-controller,
showing the original ops-api implementation and the new ops-controller implementation side by side.

## Service Lifecycle Routes

| # | Route | ops-api (original) | ops-controller (new) | Test Class |
|---|-------|-------------------|---------------------|------------|
| 1 | `POST /services/{id}/start` | `service_start()` in `services/ops-api/main.py` — shells out to `docker compose up -d` | `service_start()` in `ordo/control.py` — delegates to `broker.backend.start()` | `TestServiceStart` |
| 2 | `POST /services/{id}/stop` | `service_stop()` in `services/ops-api/main.py` — shells out to `docker compose stop` | `service_stop()` in `ordo/control.py` — delegates to `broker.backend.stop()` | `TestServiceStop` |
| 3 | `POST /services/{id}/restart` | `service_restart()` in `services/ops-api/main.py` — shells out to `docker compose restart` | `service_restart()` in `ordo/control.py` — delegates to `broker.backend.restart()` | `TestServiceRestart` |
| 4 | `GET /services/{id}/logs` | `service_logs()` in `services/ops-api/main.py` — shells out to `docker logs` | `service_logs()` in `ordo/control.py` — delegates to `broker.backend.logs()` | `TestServiceLogs` |
| 5 | `GET /services` | `list_services()` in `services/ops-api/main.py` — shells out to `docker compose ps` | `list_services()` in `ordo/control.py` — delegates to `broker.backend.list_services()` | `TestServiceList` |
| 6 | `POST /services/{id}/recreate` | `service_recreate()` in `services/ops-api/main.py` — shells out to `docker compose up -d --force-recreate` | `service_recreate()` in `ordo/control.py` — delegates to `broker.backend.recreate_service()` | `TestServiceRecreate` |

## Container Management Routes

| # | Route | ops-api (original) | ops-controller (new) | Test Class |
|---|-------|-------------------|---------------------|------------|
| 7 | `GET /containers` | `list_containers()` in `services/ops-api/main.py` — shells out to `docker ps` | `list_containers()` in `ordo/control.py` — delegates to `broker.backend.list_containers()` | `TestContainerList` |
| 8 | `GET /containers/{name}/logs` | `container_logs()` in `services/ops-api/main.py` — shells out to `docker logs` | `container_logs()` in `ordo/control.py` — delegates to `broker.backend.container_logs()` | `TestContainerLogs` |
| 9 | `POST /containers/{name}/restart` | `container_restart()` in `services/ops-api/main.py` — shells out to `docker restart` | `container_restart()` in `ordo/control.py` — delegates to `broker.backend.container_restart()` | `TestContainerRestart` |

## Stats & Monitoring Routes

| # | Route | ops-api (original) | ops-controller (new) | Test Class |
|---|-------|-------------------|---------------------|------------|
| 10 | `GET /stats/services` | `service_stats()` in `services/ops-api/main.py` — shells out to `docker stats` | `service_stats()` in `ordo/control.py` — delegates to `broker.backend.service_stats()` | `TestServiceStats` |

## MCP Routes

| # | Route | ops-api (original) | ops-controller (new) | Test Class |
|---|-------|-------------------|---------------------|------------|
| 11 | `GET /mcp/containers` | `mcp_containers()` in `services/ops-api/main.py` — shells out to `docker ps` filtered by label | `mcp_containers()` in `ordo/control.py` — delegates to `broker.backend.mcp_containers()` | `TestMcpContainers` |

## Compose Mutation Routes

| # | Route | ops-api (original) | ops-controller (new) | Test Class |
|---|-------|-------------------|---------------------|------------|
| 12 | `POST /compose/up` | `compose_up()` in `services/ops-api/main.py` — shells out to `docker compose up -d` | `compose_up()` in `ordo/control.py` — delegates to `broker.backend.compose_up()` | `TestComposeUp` |
| 13 | `POST /compose/down` | `compose_down()` in `services/ops-api/main.py` — shells out to `docker compose down` | `compose_down()` in `ordo/control.py` — delegates to `broker.backend.compose_down()` | `TestComposeDown` |
| 14 | `POST /compose/restart` | `compose_restart()` in `services/ops-api/main.py` — shells out to `docker compose restart` | `compose_restart()` in `ordo/control.py` — delegates to `broker.backend.compose_restart()` | `TestComposeRestart` |

## Parity Tests

| # | Test | Description | Test Class |
|---|------|-------------|------------|
| 15 | Response shape parity | All 14 routes return the same JSON shape as ops-api | `TestRouteParityWithOpsApi` |

## Architecture Change

The key architectural change is that ops-api routes **shelled out to Docker CLI commands** directly,
while ops-controller routes **delegate to a broker backend** (`broker.backend.*`). This enables:

1. **Testability**: MockBackend records actions without touching Docker
2. **Scoping**: DockerBackend is hard-scoped to its compose project via labels
3. **Resilience**: The broker handles lease management, eviction, and restoration
4. **Separation of concerns**: Control plane logic is pure; Docker interaction is isolated

## Test Results

All 45 tests pass:
- 14 route classes × 3-4 tests each = 44 route tests
- 1 parity test class = 1 test
- Total: 45 passed
