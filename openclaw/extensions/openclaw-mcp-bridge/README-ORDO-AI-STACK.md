# Ordo AI Stack fork of `openclaw-mcp-bridge`

**Upstream:** npm `openclaw-mcp-bridge@0.2.0`

**Change:** In addition to each server’s `*__call` proxy, this fork registers **one OpenClaw tool per discovered MCP tool** using the same namespaced id the MCP client uses (e.g. `gateway__duckduckgo__search`). That matches what models often emit and eliminates spurious `Tool not found` when the proxy-only shape was never invoked.

**Registration:** `gateway_start` and `session_start` hooks call `MCPManager.getRegisteredTools()` after `connectAll()` and `api.registerTool()` for each entry.

**Maintenance:** When bumping upstream, merge `dist/index.js` `register()` from upstream, then re-apply the `registerFlatMcpTools` block (search for `registerFlatMcpTools`).
