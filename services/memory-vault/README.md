# mcpvault-mcp

MCP server that wraps [`@bitbonsai/mcpvault`](https://github.com/bitbonsai/mcpvault) to give
Hermes/agents read+write access to the stack's **shared markdown memory vault** (the same host dir
native Obsidian browses when opened at that path).

## Build

```sh
ordo build mcp-memory-vault
```

## Pins

- Base: `node:22-bookworm-slim` (digest-pinned in the Dockerfile).
- `@bitbonsai/mcpvault`: pinned via `MCPVAULT_VERSION` build arg (exact version, never `@latest`).
- `mcp-proxy==0.12.0` + `mcp==1.30.0` (the bridge; `mcp` is bounded on purpose, 2.x breaks
  `mcp-proxy`'s imports).

## How it's wired

This is a **long-lived compose service**, `mcp-memory-vault`, on the internal `ordo-mcp-net`
network, not a gateway-spawned stdio sibling. `mcpvault` speaks stdio only, so the image bridges it
to **streamable HTTP** with a pinned `mcp-proxy`:

```
CMD ["mcp-proxy", "--host", "0.0.0.0", "--port", "9000", "--stateless", "--pass-environment", "--", "mcpvault", "/vault"]
```

`--stateless` is required because LiteLLM's outbound MCP client re-initialises per operation and
carries no session id; `--pass-environment` is required because `mcp-proxy` otherwise starts the
stdio child with an empty environment.

LiteLLM's MCP gateway on `model-gateway` dials `http://mcp-memory-vault:9000/mcp`. Registered by the
`memory-vault` `kind: mcp` plugin ([`plugin.yaml`](plugin.yaml)); `ordo render` emits it into
`out/model-gateway/mcp_servers.yaml` (the LiteLLM fragment its entrypoint merges) and
`out/mcp/servers.json` (the dashboard's server list).

## Vault mount (read-write)

The vault bind comes from the manifest's `volumes:`, compose-interpolated from
`${MEMORY_VAULT_PATH}` (rendered from `site.MEMORY_VAULT_PATH`) onto `/vault`. There is **no `:ro`
suffix**: the bind is read-write so `write_note`/`patch_note`/`delete_note` persist to disk. The
target matches the image CMD's `mcpvault /vault`. No placeholder substitution and no Docker socket
are involved.

## stdio hygiene / network

mcpvault only writes to stdout on `--version`/`--help` (early-exit paths never taken in normal
operation), so the stdio JSON-RPC channel `mcp-proxy` reads stays clean. It is pure-filesystem, and
the manifest's `network: internal` reflects that: only `model-gateway` can reach it and it can
reach nothing. The container is long-lived by construction, as a compose service.

## How the tools appear

LiteLLM namespaces tools `<litellm_name>-<tool>`, where `litellm_name` is the server id with
hyphens replaced by underscores. Hermes adds its own gateway prefix, so it sees
`gateway__memory_vault-read_note`, `gateway__memory_vault-write_note`, and so on.

## Trash / deletes

`delete_note` takes a per-call `trashMode` (`none` = permanent, `local` = `.trash/` inside the vault,
`system` = OS trash). There is no env to change the default, so agent guidance should pass
`trashMode: local` to keep deletes recoverable inside the vault rather than permanent.
