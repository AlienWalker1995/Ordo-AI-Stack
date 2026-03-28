/**
 * Plugin entry point for the OpenClaw MCP client plugin.
 *
 * Implements the OpenClaw plugin SDK contract: exports a default object
 * with a `register(api)` function that registers MCP tools via
 * `api.registerTool()`.
 *
 * @see SPEC.md section 6.4 for the plugin entry point specification.
 */
import { Type } from "@sinclair/typebox";
import { MCPManager } from "./manager/mcp-manager.js";
// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
/**
 * Convert a ConfigSchemaType to the MCPManagerConfig shape expected by MCPManager.
 *
 * @param config - The plugin configuration from OpenClaw.
 * @returns An MCPManagerConfig ready for the MCPManager constructor.
 */
function toManagerConfig(config) {
    return {
        servers: config.servers,
        toolDiscoveryInterval: config.toolDiscoveryInterval,
        maxConcurrentServers: config.maxConcurrentServers,
        debug: config.debug,
    };
}
// ---------------------------------------------------------------------------
// Plugin registration
// ---------------------------------------------------------------------------
/**
 * Register function called synchronously by OpenClaw's plugin runtime.
 *
 * Since MCP server connections are async but register() must be synchronous,
 * we register tool factories that lazily connect on first invocation.
 *
 * @param api - The OpenClaw plugin API.
 */
function register(api) {
    const config = api.pluginConfig;
    if (!config?.servers || Object.keys(config.servers).length === 0) {
        return;
    }
    const mcpManager = new MCPManager(toManagerConfig(config));
    // Register a factory for each configured server's tools.
    // The factory connects lazily on first tool call.
    let connected = false;
    const ensureConnected = async () => {
        if (connected)
            return;
        await mcpManager.connectAll();
        connected = true;
    };
    // Register a proxy tool per configured server.
    // Each tool connects lazily and forwards calls to the remote MCP server.
    for (const [serverName, serverConfig] of Object.entries(config.servers)) {
        if (serverConfig.enabled === false)
            continue;
        const prefix = serverConfig.toolPrefix ?? serverName;
        api.registerTool({
            name: `${prefix}__call`,
            label: `MCP: ${serverName}`,
            description: `Call a tool on MCP server "${serverName}" (${serverConfig.url}). Pass tool name and arguments to invoke any tool on this server.`,
            parameters: Type.Object({
                tool: Type.String({ description: "The tool name to call on this server" }),
                args: Type.Optional(Type.Record(Type.String(), Type.Unknown(), { description: "Arguments to pass to the tool" })),
            }),
            async execute(_toolCallId, params) {
                await ensureConnected();
                const toolName = params.tool;
                const args = params.args ?? {};
                try {
                    const result = await mcpManager.callTool(`${prefix}__${toolName}`, args);
                    const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
                    return {
                        content: [{ type: "text", text }],
                        details: { server: serverName, tool: toolName, result },
                    };
                }
                catch (err) {
                    const message = err instanceof Error ? err.message : String(err);
                    return {
                        content: [{ type: "text", text: `Error calling ${prefix}__${toolName}: ${message}` }],
                        details: { server: serverName, tool: toolName, error: message },
                    };
                }
            },
        });
        api.logger.info(`mcp-client: registered proxy tool ${prefix}__call for server "${serverName}"`);
    }
    // Register each discovered MCP tool as its own OpenClaw tool (e.g. gateway__duckduckgo__search).
    // Upstream only registered *ServerName*__call; models often emit the namespaced MCP id, which hit Tool not found.
    let flatToolsRegistered = false;
    /** Avoid infinite hooks if gateway never exposes tools. */
    let flatToolsRegistrationAttempts = 0;
    const MAX_FLAT_REGISTRATION_ATTEMPTS = 24;
    /** Delay helper */
    const _delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const registerFlatMcpTools = async () => {
        if (flatToolsRegistered) {
            return;
        }
        try {
            await ensureConnected();
            // On first attempt after connection, refresh tools from the gateway
            // to pick up servers that loaded after the initial connectAll().
            if (flatToolsRegistrationAttempts > 0) {
                try {
                    await mcpManager.refreshTools();
                } catch (_refreshErr) {
                    // Refresh failed — fall through to check registry as-is
                }
            }
            const discovered = mcpManager.getRegisteredTools();
            if (discovered.length === 0) {
                flatToolsRegistrationAttempts += 1;
                if (flatToolsRegistrationAttempts >= MAX_FLAT_REGISTRATION_ATTEMPTS) {
                    api.logger.warn("[mcp-bridge] registerFlatMcpTools: giving up after " +
                        String(MAX_FLAT_REGISTRATION_ATTEMPTS) +
                        " attempts with 0 tools — check mcp-gateway and data/mcp/servers.txt (include comfyui). " +
                        "Use gateway__call with tool names from the gateway until flat tools appear.");
                    flatToolsRegistered = true;
                    return;
                }
                api.logger.warn("[mcp-bridge] registerFlatMcpTools: 0 tools in registry (attempt " +
                    String(flatToolsRegistrationAttempts) + "/" + String(MAX_FLAT_REGISTRATION_ATTEMPTS) +
                    "). Retrying in 5s — MCP gateway may still be loading servers.");
                // Schedule a timed retry instead of waiting for the next hook
                setTimeout(() => { registerFlatMcpTools().catch(() => {}); }, 5000);
                return;
            }
            for (const rt of discovered) {
                const srvCfg = config.servers[rt.serverName];
                const pfx = srvCfg?.toolPrefix ?? rt.serverName;
                api.registerTool({
                    name: rt.namespacedName,
                    label: `MCP ${rt.serverName}: ${rt.originalName}`,
                    description: `${rt.description ?? ""}\n\nSame as \`${pfx}__call\` with tool "${rt.originalName}" and args for parameters.`,
                    parameters: Type.Record(Type.String(), Type.Unknown(), {
                        description: "Arguments for this MCP tool (see injected MCP tool list).",
                    }),
                    async execute(_toolCallId, params) {
                        await ensureConnected();
                        try {
                            const result = await mcpManager.callTool(rt.namespacedName, params);
                            const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
                            return {
                                content: [{ type: "text", text }],
                                details: { server: rt.serverName, tool: rt.originalName, result },
                            };
                        }
                        catch (err) {
                            const message = err instanceof Error ? err.message : String(err);
                            return {
                                content: [{ type: "text", text: `Error calling ${rt.namespacedName}: ${message}` }],
                                details: { server: rt.serverName, tool: rt.originalName, error: message },
                            };
                        }
                    },
                });
                api.logger.info(`mcp-client: registered flat tool ${rt.namespacedName}`);
            }
            flatToolsRegistered = true;
        }
        catch (err) {
            api.logger.warn("[mcp-bridge] registerFlatMcpTools failed:", err);
        }
    };
    const flatToolsHook = async () => {
        await registerFlatMcpTools();
    };
    api.registerHook("gateway_start", flatToolsHook, { name: "mcp-flat-tools-gateway", description: "Expose namespaced MCP tools as OpenClaw tools" });
    api.registerHook("session_start", flatToolsHook, { name: "mcp-flat-tools-session", description: "Fallback if gateway_start is unavailable" });
    // Eager: kick off flat tool discovery synchronously (as a floating promise).
    // Hooks may not fire on all OpenClaw versions; calling directly ensures discovery runs.
    api.logger.info("[mcp-bridge] starting eager flat tool discovery");
    registerFlatMcpTools().then(() => {
        api.logger.info("[mcp-bridge] eager discovery done, registered=" + String(flatToolsRegistered));
    }).catch((err) => {
        api.logger.warn("[mcp-bridge] eager discovery error: " + String(err));
    });
    // Register shutdown hook
    api.registerHook("gateway_stop", async () => {
        if (connected) {
            await mcpManager.disconnectAll();
        }
    }, { name: "mcp-client-shutdown", description: "Disconnect all MCP servers" });
    // Register schema injection hook (injects MCP tool schemas into agent context)
    if (config.injectSchemas !== false) {
        api.on("before_prompt_build", async () => {
            try {
                await ensureConnected();
                const allSchemas = [];
                for (const [serverName, serverConfig] of Object.entries(config.servers)) {
                    if (serverConfig.enabled === false)
                        continue;
                    try {
                        const tools = await mcpManager.listTools(serverName);
                        if (tools.length > 0) {
                            const formatted = tools.map((tool) => {
                                const props = tool.inputSchema?.properties;
                                const params = props
                                    ? Object.entries(props)
                                        .map(([name, schema]) => {
                                        const s = schema;
                                        const required = tool.inputSchema.required?.includes(name) ? " (required)" : "";
                                        const description = (s.description ?? s.type ?? "");
                                        return `  - **${name}**${required}: ${description}`;
                                    })
                                        .join("\n")
                                    : "  (no parameters)";
                                return `### ${tool.name}\n${tool.description ?? ""}\n\nParameters:\n${params}`;
                            }).join("\n\n");
                            allSchemas.push(`## MCP Server: ${serverName}\n\n${formatted}`);
                        }
                    }
                    catch (err) {
                        api.logger.warn(`[mcp-bridge] Failed to fetch schemas from ${serverName}:`, err);
                    }
                }
                if (allSchemas.length > 0) {
                    const firstServerName = Object.keys(config.servers)[0];
                    const firstConfig = config.servers[firstServerName];
                    const prefix = firstConfig?.toolPrefix ?? firstServerName;
                    return {
                        appendSystemContext: `\n\n## MCP Tools Available\n\nThe following tools are available via MCP servers. Use the \`${prefix}__call\` tool with the tool name and arguments.\n\n${allSchemas.join("\n\n")}`,
                    };
                }
            }
            catch (error) {
                api.logger.warn("[mcp-bridge] Failed to inject MCP schemas:", error);
            }
            return {};
        }, { priority: 5 });
        api.logger.info("mcp-client: registered schema injection hook");
    }
}
/**
 * Standalone plugin object with an async initialize() method.
 *
 * Connects to all configured MCP servers eagerly, discovers tools,
 * and returns them as ToolDefinition objects with bound execute functions.
 *
 * @param context - The plugin context containing configuration.
 * @returns A PluginResult with tools, shutdown, and config-change callbacks.
 */
export const plugin = {
    async initialize(context) {
        const config = context.config;
        if (!config?.servers || Object.keys(config.servers).length === 0) {
            return { tools: [], onShutdown: async () => { } };
        }
        const mcpManager = new MCPManager(toManagerConfig(config));
        await mcpManager.connectAll();
        const buildTools = () => {
            const registeredTools = mcpManager.getRegisteredTools();
            return registeredTools.map((rt) => ({
                name: rt.namespacedName,
                description: rt.description,
                inputSchema: rt.inputSchema,
                execute: async (args) => {
                    return mcpManager.callTool(rt.namespacedName, args);
                },
            }));
        };
        const tools = buildTools();
        return {
            tools,
            onShutdown: async () => {
                await mcpManager.disconnectAll();
            },
            onConfigChange: async (newConfig) => {
                await mcpManager.reconcile(toManagerConfig(newConfig));
            },
        };
    },
};
// ---------------------------------------------------------------------------
// Default Export
// ---------------------------------------------------------------------------
export default { register };
// ---------------------------------------------------------------------------
// Re-exports for external consumers
// ---------------------------------------------------------------------------
// Manager layer
export { MCPManager } from "./manager/mcp-manager.js";
export { ToolRegistry } from "./manager/tool-registry.js";
// Transport layer
export { StreamableHTTPTransport } from "./transport/streamable-http.js";
export { StdioTransport } from "./transport/stdio.js";
export { SSEParser, parseSSEStream } from "./transport/sse-parser.js";
// Config and types
export { configSchema } from "./config-schema.js";
export { MCPError } from "./types.js";
//# sourceMappingURL=index.js.map