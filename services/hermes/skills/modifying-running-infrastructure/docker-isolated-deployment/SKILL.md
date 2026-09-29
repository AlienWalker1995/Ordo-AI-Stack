---
name: docker-isolated-deployment
description: RETIRED. Hermes no longer creates, runs or deploys containers. Read this when asked to host, deploy, spin up or run an app or a container.
---

# Docker isolated deployment (retired)

Shipped read-only in the agent image (repo:
`services/hermes/skills/modifying-running-infrastructure/docker-isolated-deployment/`). It
replaces the older workflow of the same name, which built and ran containers over the raw Docker
socket and restarted services through routes your token no longer has.

The 2026-08-09 "full Docker control" grant is retired (hostile audit SEC-1). You cannot create,
run, build or deploy containers, and you must not try to by other means.

When asked to host or deploy something:

1. Do the repo work if there is any (the app's code, its Dockerfile, a compose file in the
   project's own repo), on a branch, with its tests.
2. Tell the operator plainly that deploying it is a host step, and give the exact commands to run
   there. Never say it is deployed, running or reachable unless you checked it yourself after the
   operator ran them.
3. For an Ordo service, the host step is `ordo apply`. For another stack in `managed_projects:`,
   you may restart its existing containers (`restart_project_container`), but creating new ones is
   still the operator's.
