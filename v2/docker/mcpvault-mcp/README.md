# mcpvault-mcp

Gateway-spawned, stdio-transport MCP server that wraps [`@bitbonsai/mcpvault`](https://github.com/bitbonsai/mcpvault)
to give Hermes/agents read+write access to the stack's **shared markdown memory vault** (the same
host dir native Obsidian browses when opened at that path).

## Build

```sh
docker build -t ordo-v2/mcpvault-mcp:latest v2/docker/mcpvault-mcp
```

Registered by the `memory-vault` kind=mcp plugin (`v2/plugins/memory-vault/plugin.yaml`); `ordo render`
emits it into `out/mcp/servers.txt` + `out/mcp/registry-custom.yaml`.

## Pins

- Base: `node:22-bookworm-slim` (digest-pinned in the Dockerfile).
- `@bitbonsai/mcpvault`: pinned via `MCPVAULT_VERSION` build arg (exact version, never `@latest`).

## Vault mount (read-write)

The gateway spawns this as a sibling container via the host `docker.sock`, so the vault volume source
is a **host path**. The catalog entry uses `PLACEHOLDER_MEMORY_VAULT_PATH:/vault`; `gateway-wrapper.sh`
substitutes it from the gateway's `MEMORY_VAULT_PATH` env (rendered from `site.MEMORY_VAULT_PATH`).
There is **no `:ro` suffix** — the bind is read-write so `write_note`/`patch_note`/`delete_note`
persist to disk. The image CMD is `mcpvault /vault`.

## stdio hygiene / network

mcpvault only writes to stdout on `--version`/`--help` (early-exit paths never taken in normal
operation), so the stdio JSON-RPC channel stays clean. It is pure-filesystem — the catalog sets
`longLived: true` (keep warm) and `disableNetwork: true` (no egress).

## Trash / deletes

`delete_note` takes a per-call `trashMode` (`none` = permanent, `local` = `.trash/` inside the vault,
`system` = OS trash). There is no env to change the default, so agent guidance should pass
`trashMode: local` to keep deletes recoverable inside the vault rather than permanent.
