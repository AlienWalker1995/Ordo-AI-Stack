# worker (render/publish job worker)

V2's background job worker, referenced by the `worker` plugin as `ordo/worker:latest`. V1 builds
it from the repo root with `worker/Dockerfile` (it bundles the dashboard package + worker deps) —
it polls the dashboard job store and drives ComfyUI render + publish flows. Project buildable image,
so `ordo preflight` reports a missing one as "build first".

## Build
Built from the operator's authoritative context (dashboard + worker sources), tagging the V2 image:
```
docker build -f C:/dev/ordo-ai-stack/worker/Dockerfile -t ordo/worker:latest C:/dev/ordo-ai-stack
```

Referenced (not duplicated) so the worker + dashboard sources stay a single source of truth.
