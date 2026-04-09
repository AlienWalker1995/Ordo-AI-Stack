/**
 * TypeBox configuration schema for the MCP client plugin.
 *
 * Defines the full configuration shape including per-server settings
 * (transport, authentication, timeouts) and global plugin settings.
 *
 * @see SPEC.md section 6.2 for the canonical schema definition.
 */
import { Type } from "@sinclair/typebox";
/**
 * OAuth 2.1 authentication configuration for a single MCP server.
 *
 * Supports pre-registered credentials, authorization server override,
 * and custom scope requests.
 */
export const ServerAuthConfig = Type.Object({
    /** Pre-registered OAuth client ID (optional). */
    clientId: Type.Optional(Type.String()),
    /** OAuth client secret for confidential clients (optional). */
    clientSecret: Type.Optional(Type.String()),
    /** Override the authorization server URL (optional, normally auto-discovered via RFC 9728). */
    authorizationServerUrl: Type.Optional(Type.String()),
    /** Custom scopes to request (optional, normally derived from WWW-Authenticate). */
    scopes: Type.Optional(Type.Array(Type.String())),
});
/**
 * Configuration for a single MCP server connection.
 *
 * Covers HTTP and stdio transports, authentication (API key or OAuth 2.1),
 * tool namespacing, and connection timeouts.
 */
export const MCPServerConfig = Type.Object({
    /** Whether this server is enabled. */
    enabled: Type.Boolean({ default: true }),
    /** MCP server endpoint URL (required for HTTP transport). */
    url: Type.String({ format: "uri", description: "MCP server endpoint URL" }),
    /** Transport type: HTTP (Streamable HTTP) or stdio (subprocess). */
    transport: Type.Optional(Type.Union([Type.Literal("http"), Type.Literal("stdio")], {
        default: "http",
    })),
    /** Command to run for stdio transport. */
    command: Type.Optional(Type.String()),
    /** Arguments for the stdio command. */
    args: Type.Optional(Type.Array(Type.String())),
    /** Environment variables for the stdio subprocess. */
    env: Type.Optional(Type.Record(Type.String(), Type.String())),
    /** OAuth 2.1 authentication configuration. */
    auth: Type.Optional(ServerAuthConfig),
    /** Static API key sent as a Bearer token (simpler alternative to OAuth). */
    apiKey: Type.Optional(Type.String()),
    /** Namespace prefix for tools from this server (defaults to server name). */
    toolPrefix: Type.Optional(Type.String()),
    /** Connection timeout in milliseconds. */
    connectTimeoutMs: Type.Optional(Type.Number({ default: 10000 })),
    /** Per-request timeout in milliseconds. */
    requestTimeoutMs: Type.Optional(Type.Number({ default: 30000 })),
});
/**
 * Top-level plugin configuration schema.
 *
 * Contains a map of named server configurations and global plugin settings.
 */
export const configSchema = Type.Object({
    /** Map of server name to server configuration. */
    servers: Type.Record(Type.String(), MCPServerConfig),
    /** Interval in milliseconds to re-discover tools from all servers. */
    toolDiscoveryInterval: Type.Optional(Type.Number({
        default: 300000,
        description: "Re-discover tools every N ms",
    })),
    /** Maximum number of simultaneous server connections. */
    maxConcurrentServers: Type.Optional(Type.Number({ default: 20 })),
    /** Enable debug logging. */
    debug: Type.Optional(Type.Boolean({ default: false })),
    /** Automatically inject MCP tool schemas into agent context. */
    injectSchemas: Type.Optional(Type.Boolean({
        default: false,
        description: "Automatically inject full MCP tool schemas into agent context. Keep off by default and use discovery instead.",
    })),
    /** Register each MCP tool as its own flat OpenClaw tool (e.g. gateway__tavily_search).
     *  When false (default), only `gateway__call` is registered — the model uses it to invoke
     *  any MCP tool by name. This avoids the heavy eager tool discovery that blocks session
     *  startup and embedded helpers like slug-gen. */
    flatTools: Type.Optional(Type.Boolean({
        default: false,
        description: "Register individual flat tools per MCP tool (eager discovery). When false, only gateway__call is available.",
    })),
    ggufMode: Type.Optional(Type.Boolean({
        default: false,
        description: "Enable GGUF-mode: lower retry thresholds and tighter response caps for local quantized models. Auto-detected from OPENCLAW_MODEL env var if not set.",
    })),
});
//# sourceMappingURL=config-schema.js.map
