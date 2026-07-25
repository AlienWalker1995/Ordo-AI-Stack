# worker (render/publish job worker)

V2's background job worker, referenced by the `worker` plugin (`services/worker/plugin.yaml`) as
`ordo/worker:latest`. It polls the dashboard job store and drives ComfyUI render + publish flows.
It bundles the dashboard package + worker deps, so it builds from the repo ROOT (it COPYs both
`services/worker/` and the `services/v1-parity/dashboard/` modules it imports). Project buildable
image, so `ordo preflight` reports a missing one as "build first".

## Build

Built from the repo-root context (the root `.dockerignore` allowlists the render-data + service
sources it needs), tagging the V2 image:

```
docker build -f services/worker/Dockerfile -t ordo/worker:latest .
```

The dashboard package is referenced (not duplicated) so the worker + dashboard sources stay a
single source of truth.
