# codebase-memory-ui (code knowledge-graph UI)

V2's Codebase-Memory 3D graph UI, referenced by the `codebase-memory-ui` plugin as
`ordo-v2/codebase-memory-ui:latest`. V1 builds it from `C:\dev\ordo-ai-stack\codebase-memory-ui`
— an nginx wrapper that serves the UI under `/codebase-memory/` and indexes the read-only code
root in-process. Project buildable image, so `ordo preflight` reports a missing one as "build first".

## Build
```
docker build -t ordo-v2/codebase-memory-ui:latest C:/dev/ordo-ai-stack/codebase-memory-ui
```

Referenced (not duplicated) so the V1 build context stays the single source of truth. Opt-in behind
the `codebase-memory` compose profile.
