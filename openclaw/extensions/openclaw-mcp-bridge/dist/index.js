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
import { promises as fs } from "node:fs";
import path from "node:path";
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

function resolveProxyToolName(mcpManager, prefix, rawToolName) {
    const toolName = typeof rawToolName === "string" ? rawToolName.trim() : "";
    const candidates = [];
    const addCandidate = (candidate) => {
        if (typeof candidate !== "string" || candidate.length === 0) {
            return;
        }
        if (!candidates.includes(candidate)) {
            candidates.push(candidate);
        }
    };
    if (toolName.startsWith(`${prefix}__`)) {
        addCandidate(toolName);
    }
    addCandidate(`${prefix}__${toolName}`);
    if (toolName.includes("__")) {
        addCandidate(`${prefix}__${toolName.replace(/__/g, "_")}`);
        const parts = toolName.split("__").filter((part) => part.length > 0);
        if (parts.length > 1) {
            const withoutFirst = parts.slice(1).join("__");
            addCandidate(`${prefix}__${withoutFirst}`);
            addCandidate(`${prefix}__${withoutFirst.replace(/__/g, "_")}`);
            addCandidate(`${prefix}__${parts[parts.length - 1]}`);
        }
    }
    const registered = new Set(mcpManager.getRegisteredTools().map((tool) => tool.namespacedName));
    for (const candidate of candidates) {
        if (registered.has(candidate)) {
            return candidate;
        }
    }
    return candidates[0] ?? `${prefix}__${toolName}`;
}

function toFlatToolParametersSchema(rt) {
    const schema = rt?.inputSchema;
    if (schema && typeof schema === "object" && !Array.isArray(schema)) {
        return buildLooseToolSchema(schema);
    }
    return Type.Record(Type.String(), Type.Unknown(), {
        description: "Arguments for this MCP tool (see injected MCP tool list).",
    });
}

function buildLooseToolSchema(schema) {
    if (!schema || typeof schema !== "object" || Array.isArray(schema)) {
        return schema;
    }
    if (Array.isArray(schema.anyOf)) {
        const mapped = schema.anyOf.map((entry) => buildLooseToolSchema(entry));
        // object-string fallback: if anyOf contains an object variant, also accept a string
        // so models that emit JSON object strings (e.g. overrides: "{...}") pass validation.
        const hasObjectType = mapped.some((v) => v && typeof v === "object" && !Array.isArray(v) && v.type === "object");
        const hasStringFallback = mapped.some((v) => v && typeof v === "object" && !Array.isArray(v) && v.type === "string");
        if (hasObjectType && !hasStringFallback) {
            mapped.push({
                type: "string",
                description: "object-string fallback: pass a JSON object string; coerceFlatToolValue repairs it before forwarding.",
            });
        }
        return { ...schema, anyOf: mapped };
    }
    if (Array.isArray(schema.oneOf)) {
        return {
            ...schema,
            oneOf: schema.oneOf.map((entry) => buildLooseToolSchema(entry)),
        };
    }
    if (Array.isArray(schema.allOf)) {
        return {
            ...schema,
            allOf: schema.allOf.map((entry) => buildLooseToolSchema(entry)),
        };
    }
    if (schema.type === "object") {
        const properties = schema.properties && typeof schema.properties === "object"
            ? Object.fromEntries(Object.entries(schema.properties).map(([key, value]) => [key, buildLooseToolSchema(value)]))
            : schema.properties;
        const additionalProperties = schema.additionalProperties && typeof schema.additionalProperties === "object"
            ? buildLooseToolSchema(schema.additionalProperties)
            : schema.additionalProperties;
        const loosened = { ...schema, properties, additionalProperties };
        const stringFallback = {
            type: "string",
            description: "object-string fallback: pass a JSON object string; coerceFlatToolValue repairs it before forwarding.",
        };
        const { type: _type, properties: _props, additionalProperties: _ap, ...rest } = schema;
        return { ...rest, anyOf: [loosened, stringFallback] };
    }
    if (schema.type === "array" && schema.items && typeof schema.items === "object" && !Array.isArray(schema.items)) {
        return {
            ...schema,
            items: buildLooseToolSchema(schema.items),
        };
    }
    if (schema.type === "integer" || schema.type === "number" || schema.type === "boolean") {
        const stringFallback = {
            type: "string",
            description: "String fallback accepted so the bridge can sanitize and coerce model-emitted arguments before execution.",
        };
        const variants = Array.isArray(schema.anyOf) ? [...schema.anyOf, stringFallback] : [schema, stringFallback];
        return {
            ...schema,
            anyOf: variants,
        };
    }
    return schema;
}

function sanitizeModelToolText(raw) {
    if (typeof raw !== "string") {
        return "";
    }
    return raw
        .replace(/[â€œâ€]/g, '"')
        .replace(/[â€˜â€™]/g, "'")
        .replaceAll('<|"|>', '"')
        .replaceAll("<|'|>", "'")
        .replaceAll("<|`|>", "`")
        .replaceAll("<|\\n|>", "\n")
        .replace(/<\|(.)\|>/g, "$1")
        .trim();
}

function collectStringFragments(value) {
    const fragments = [];
    const visit = (current) => {
        if (current == null) {
            return;
        }
        if (typeof current === "string") {
            const text = sanitizeModelToolText(current);
            if (text) {
                fragments.push(text);
            }
            return;
        }
        if (Array.isArray(current)) {
            for (const item of current) {
                visit(item);
            }
            return;
        }
        if (typeof current === "object") {
            for (const [key, nested] of Object.entries(current)) {
                if (typeof key === "string" && key) {
                    fragments.push(key);
                }
                visit(nested);
            }
        }
    };
    visit(value);
    return fragments;
}

function extractQuotedField(text, fieldNames) {
    for (const fieldName of fieldNames) {
        const quoted = new RegExp(`["']${fieldName}["']\\s*[:=]\\s*["']([^"'\\n]+)["']`, "i");
        const quotedMatch = text.match(quoted);
        if (quotedMatch?.[1]) {
            return quotedMatch[1].trim();
        }
        const bare = new RegExp(`\\b${fieldName}\\b\\s*[:=]\\s*([A-Za-z0-9_.:-]+)`, "i");
        const bareMatch = text.match(bare);
        if (bareMatch?.[1]) {
            return bareMatch[1].trim();
        }
    }
    return "";
}

function extractBalancedObject(text, anchorPattern) {
    const anchorMatch = text.match(anchorPattern);
    if (!anchorMatch || anchorMatch.index == null) {
        return "";
    }
    const start = text.indexOf("{", anchorMatch.index);
    if (start < 0) {
        return "";
    }
    let depth = 0;
    let inString = false;
    let quote = "";
    let escaped = false;
    for (let index = start; index < text.length; index += 1) {
        const char = text[index];
        if (inString) {
            if (escaped) {
                escaped = false;
                continue;
            }
            if (char === "\\") {
                escaped = true;
                continue;
            }
            if (char === quote) {
                inString = false;
                quote = "";
            }
            continue;
        }
        if (char === '"' || char === "'") {
            inString = true;
            quote = char;
            continue;
        }
        if (char === "{") {
            depth += 1;
        }
        else if (char === "}") {
            depth -= 1;
            if (depth === 0) {
                return text.slice(start, index + 1);
            }
        }
    }
    return text.slice(start);
}

function recoverProxyInvocation(params) {
    const fragments = collectStringFragments(params);
    if (fragments.length === 0) {
        return null;
    }
    const combined = fragments.join("\n");
    const toolName = extractQuotedField(combined, ["tool", "toolName", "name", "namespacedTool"]);
    const argsText = extractBalancedObject(combined, /["']args["']\s*[:=]\s*/i) || extractBalancedObject(combined, /\bargs\b\s*[:=]\s*/i);
    const invocationText = extractBalancedObject(combined, /gateway__call\s*\(/i) || extractBalancedObject(combined, /call:gateway__call\s*/i);
    for (const candidate of [argsText, invocationText].filter(Boolean)) {
        try {
            const parsed = coerceToolArgs(candidate);
            const recoveredTool = toolName || (typeof parsed.tool === "string" ? parsed.tool.trim() : "");
            const recoveredArgs = parsed.args && typeof parsed.args === "object" && !Array.isArray(parsed.args)
                ? parsed.args
                : parsed;
            if (recoveredTool) {
                return { toolName: recoveredTool, args: recoveredArgs };
            }
        }
        catch { }
    }
    if (toolName) {
        const inlineArgs = {};
        for (const key of ["query", "input", "prompt", "url", "text", "path"]) {
            const value = extractQuotedField(combined, [key]);
            if (value) {
                inlineArgs[key] = value;
            }
        }
        return { toolName, args: inlineArgs };
    }
    return null;
}

function coerceToolArgs(raw) {
    if (raw == null) {
        return {};
    }
    if (typeof raw === "object" && !Array.isArray(raw)) {
        return raw;
    }
    if (typeof raw !== "string") {
        throw new Error("args must be an object or JSON object string");
    }
    let text = sanitizeModelToolText(raw);
    if (!text) {
        return {};
    }
    text = text
        .replace(/[“”]/g, '"')
        .replace(/[‘’]/g, "'")
        .replaceAll('<|"|>', '"')
        .replaceAll("<|'|>", "'")
        .replaceAll("<|`|>", "`")
        .replaceAll("<|\\n|>", "\n")
        .replace(/<\|(.)\|>/g, "$1");
    const firstBrace = text.indexOf("{");
    const lastBrace = text.lastIndexOf("}");
    if ((firstBrace > 0 || lastBrace !== text.length - 1) && firstBrace >= 0 && lastBrace > firstBrace) {
        text = text.slice(firstBrace, lastBrace + 1).trim();
    }
    // Strip leading/trailing quotes from models that double-stringify args
    if ((text.startsWith('"') && text.endsWith('"')) || (text.startsWith("'") && text.endsWith("'"))) {
        try {
            const inner = JSON.parse(text);
            if (typeof inner === "string") {
                text = inner.trim();
            }
        }
        catch { /* not a quoted string, continue with original */ }
    }
    // Strip a stray leading quote Gemma emits before { (e.g. <|"|>{...} → "{..." → {...)
    if (text.startsWith('"') && text.length > 1 && (text[1] === '{' || text[1] === '[')) {
        text = text.slice(1);
    }
    if (!text.startsWith("{")) {
        text = `{${text}}`;
    }
    let parsed;
    try {
        parsed = JSON.parse(text);
    }
    catch (err) {
        const repaired = text
            .replace(/\\"/g, '"')
            .replace(/\\n/g, "\n")
            .replace(/\]\s*$/, "}")
            .replace(/,\s*}/g, "}")
            .replace(/([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)/g, '$1"$2"$3')
            .replace(/:\s*'([^']*)'/g, ': "$1"')
            .replace(/\bTrue\b/g, "true")
            .replace(/\bFalse\b/g, "false")
            .replace(/\bNone\b/g, "null");
        const balanced = repaired.endsWith("}") ? repaired : `${repaired}}`;
        try {
            parsed = JSON.parse(balanced);
        }
        catch (_repairErr) {
            const msg = err instanceof Error ? err.message : String(err);
            throw new Error(`args JSON parse failed: ${msg}`);
        }
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("args must resolve to an object");
    }
    if (parsed.args && typeof parsed.args === "object" && !Array.isArray(parsed.args)) {
        return parsed.args;
    }
    return parsed;
}

function coerceObjectField(value) {
    if (typeof value !== "string") {
        return value;
    }
    try {
        return coerceToolArgs(value);
    }
    catch {
        return value;
    }
}

function coerceFlatToolValue(value, schema) {
    if (value == null || !schema || typeof schema !== "object" || Array.isArray(schema)) {
        if (typeof value === "string") {
            return sanitizeModelToolText(value);
        }
        if (Array.isArray(value)) {
            return value.map((item) => coerceFlatToolValue(item, null));
        }
        if (value && typeof value === "object") {
            return Object.fromEntries(Object.entries(value).map(([key, nested]) => [key, coerceFlatToolValue(nested, null)]));
        }
        return value;
    }
    if (Array.isArray(schema.anyOf)) {
        for (const option of schema.anyOf) {
            const coerced = coerceFlatToolValue(value, option);
            if (coerced !== value || option?.type === typeof coerced) {
                return coerced;
            }
        }
    }
    if (Array.isArray(schema.oneOf)) {
        for (const option of schema.oneOf) {
            const coerced = coerceFlatToolValue(value, option);
            if (coerced !== value || option?.type === typeof coerced) {
                return coerced;
            }
        }
    }
    if (schema.type === "string") {
        return typeof value === "string" ? sanitizeModelToolText(value) : value;
    }
    if (schema.type === "integer" || schema.type === "number") {
        if (typeof value === "string") {
            const cleaned = sanitizeModelToolText(value).replace(/,/g, "").replace(/[\])"\']+$/, "").trim();
            if (/^-?\d+$/.test(cleaned) && schema.type === "integer") {
                return Number.parseInt(cleaned, 10);
            }
            if (/^-?(?:\d+\.?\d*|\.\d+)$/.test(cleaned)) {
                const parsed = Number.parseFloat(cleaned);
                if (!Number.isNaN(parsed)) {
                    return parsed;
                }
            }
            return cleaned;
        }
        return value;
    }
    if (schema.type === "boolean" && typeof value === "string") {
        const cleaned = sanitizeModelToolText(value).toLowerCase();
        if (cleaned === "true") {
            return true;
        }
        if (cleaned === "false") {
            return false;
        }
        return cleaned;
    }
    if (schema.type === "array") {
        if (!Array.isArray(value)) {
            return value;
        }
        return value.map((item) => coerceFlatToolValue(item, schema.items ?? null));
    }
    if (schema.type === "object") {
        if (!value || typeof value !== "object" || Array.isArray(value)) {
            if (typeof value === "string") {
                return coerceObjectField(value);
            }
            return value;
        }
        const properties = schema.properties && typeof schema.properties === "object" ? schema.properties : {};
        const additionalSchema = schema.additionalProperties && typeof schema.additionalProperties === "object"
            ? schema.additionalProperties
            : null;
        return Object.fromEntries(Object.entries(value).map(([key, nested]) => {
            const propertySchema = Object.prototype.hasOwnProperty.call(properties, key) ? properties[key] : additionalSchema;
            return [key, coerceFlatToolValue(nested, propertySchema)];
        }));
    }
    return typeof value === "string" ? sanitizeModelToolText(value) : value;
}

function coerceFlatToolParams(params, schema) {
    if (!params || typeof params !== "object" || Array.isArray(params)) {
        return params;
    }
    return coerceFlatToolValue(params, schema);
}

function coerceToolName(raw, prefix) {
    if (typeof raw === "string" && raw.trim()) {
        return raw.trim();
    }
    if (typeof raw === "object" && raw && !Array.isArray(raw)) {
        const candidate = raw.tool ?? raw.toolName ?? raw.name ?? raw.namespacedTool;
        if (typeof candidate === "string" && candidate.trim()) {
            return candidate.trim();
        }
        const nested = raw.args;
        if (typeof nested === "string") {
            try {
                const parsedNested = coerceToolArgs(nested);
                const nestedCandidate = parsedNested.tool ?? parsedNested.toolName ?? parsedNested.name ?? parsedNested.namespacedTool;
                if (typeof nestedCandidate === "string" && nestedCandidate.trim()) {
                    return nestedCandidate.trim();
                }
            }
            catch { }
        }
        if (nested && typeof nested === "object" && !Array.isArray(nested)) {
            const nestedCandidate = nested.tool ?? nested.toolName ?? nested.name ?? nested.namespacedTool;
            if (typeof nestedCandidate === "string" && nestedCandidate.trim()) {
                return nestedCandidate.trim();
            }
        }
    }
    throw new Error(`missing required tool name for ${prefix}__call; provide "tool" with the raw MCP tool name or run ${prefix}__discover first`);
}

function compactToolMetadata(rt, includeSchema = false) {
    const schema = rt?.inputSchema && typeof rt.inputSchema === "object" && !Array.isArray(rt.inputSchema)
        ? rt.inputSchema
        : {};
    const properties = schema.properties && typeof schema.properties === "object" ? schema.properties : {};
    const required = Array.isArray(schema.required) ? new Set(schema.required) : new Set();
    const argumentsSummary = Object.entries(properties).map(([name, spec]) => ({
        name,
        required: required.has(name),
        type: spec?.type ?? "unknown",
        description: spec?.description ?? "",
    }));
    const metadata = {
        server: rt.serverName,
        tool: rt.originalName,
        namespacedTool: rt.namespacedName,
        description: rt.description ?? "",
        arguments: argumentsSummary,
    };
    if (includeSchema) {
        metadata.inputSchema = schema;
    }
    return metadata;
}

function buildSchemaContext(config, allSchemas, flatToolsEnabled) {
    if (allSchemas.length === 0) {
        return "";
    }
    const firstServerName = Object.keys(config.servers)[0];
    const firstConfig = config.servers[firstServerName];
    const prefix = firstConfig?.toolPrefix ?? firstServerName;
    const usage = flatToolsEnabled
        ? `Use the flat \`gateway__...\` tools when available. If you use \`${prefix}__call\`, the inner \`tool\` value must be the raw tool name without a \`gateway__\` prefix.`
        : `Use only \`${prefix}__call\`. The inner \`tool\` value must be the raw MCP tool name without a \`gateway__\` prefix.`;
    return `\n\n## MCP Tools Available\n\n${usage}\n\nFor unattended or cron runs: do not emit progress chatter such as "Let me check..." or "Fetching more...". Keep assistant text empty while calling tools, then emit exactly one final non-empty assistant message.\n\n${allSchemas.join("\n\n")}`;
}

const OPENCLAW_HOME = process.env.OPENCLAW_HOME?.trim() || "/home/node/.openclaw";
const SESSION_STATUS_DIR = path.join(OPENCLAW_HOME, "agents", "main", "session-status");
const SESSION_TRANSCRIPT_DIR = path.join(OPENCLAW_HOME, "agents", "main", "sessions");
const STATUS_REQUEST_RE = /^\s*(status|progress|update|what'?s the status)\s*$/i;
const CONTINUE_REQUEST_RE = /^\s*(ok(?:ay)?\s+)?(continue|resume|go on|keep going|proceed)\s*$/i;
const CONTAMINATION_MARKERS = [
    "telegram",
    "google sheets",
    "google drive",
    "google apps script",
    "creatomate",
    "runway",
    "suno",
    "n8n",
];

function trimText(value, maxChars = 800) {
    const text = typeof value === "string" ? value.trim() : "";
    if (!text) {
        return "";
    }
    return text.length > maxChars ? `${text.slice(0, maxChars - 1)}…` : text;
}

function extractText(value) {
    if (typeof value === "string") {
        return value;
    }
    if (Array.isArray(value)) {
        return value.map((item) => extractText(item)).filter(Boolean).join("\n");
    }
    if (!value || typeof value !== "object") {
        return "";
    }
    if (typeof value.text === "string") {
        return value.text;
    }
    if (Array.isArray(value.content)) {
        return value.content.map((item) => extractText(item)).filter(Boolean).join("\n");
    }
    if (value.message) {
        return extractText(value.message);
    }
    if (value.data) {
        return extractText(value.data);
    }
    return "";
}

function extractToolName(value) {
    if (!value || typeof value !== "object") {
        return "";
    }
    return trimText(value.toolName ?? value.name ?? value.tool ?? value.message?.toolName ?? "", 120);
}

function extractSessionIdentifiers(value) {
    if (!value || typeof value !== "object") {
        return { sessionId: "", sessionKey: "" };
    }
    const sessionId = trimText(value.sessionId ?? value.session?.id ?? value.data?.sessionId ?? value.payload?.sessionId ?? "", 200);
    const sessionKey = trimText(value.sessionKey ?? value.session?.key ?? value.data?.sessionKey ?? value.payload?.sessionKey ?? "", 300);
    return { sessionId, sessionKey };
}

function safeStatusKey(sessionId, sessionKey) {
    const raw = sessionId || sessionKey;
    if (!raw) {
        return "";
    }
    return raw.replace(/[^A-Za-z0-9._-]+/g, "_");
}

async function ensureSessionStatusDir() {
    await fs.mkdir(SESSION_STATUS_DIR, { recursive: true });
}

async function readSessionStatus(sessionId, sessionKey) {
    const key = safeStatusKey(sessionId, sessionKey);
    if (!key) {
        return {};
    }
    try {
        const raw = await fs.readFile(path.join(SESSION_STATUS_DIR, `${key}.json`), "utf8");
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : {};
    }
    catch {
        return {};
    }
}

async function writeSessionStatus(sessionId, sessionKey, patch) {
    const key = safeStatusKey(sessionId, sessionKey);
    if (!key || !patch || typeof patch !== "object") {
        return {};
    }
    await ensureSessionStatusDir();
    const current = await readSessionStatus(sessionId, sessionKey);
    const next = {
        ...current,
        ...patch,
        updatedAt: new Date().toISOString(),
    };
    await fs.writeFile(path.join(SESSION_STATUS_DIR, `${key}.json`), JSON.stringify(next, null, 2), "utf8");
    return next;
}

// ---------------------------------------------------------------------------
// Retry state — per-session-per-tool failure tracking
// ---------------------------------------------------------------------------
const RETRY_TTL_MS = 30 * 60 * 1000; // 30 minutes
const RETRY_DIR = path.join(SESSION_STATUS_DIR, "retry");

function retryStatePath(sessionKey, toolSlug) {
    const safeKey = (sessionKey || "default").replace(/[^A-Za-z0-9._-]+/g, "_");
    const safeSlug = toolSlug.replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 60);
    return path.join(RETRY_DIR, `${safeKey}__${safeSlug}.json`);
}

async function readRetryState(sessionKey, toolSlug) {
    try {
        const raw = await fs.readFile(retryStatePath(sessionKey, toolSlug), "utf8");
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object") {
            return null;
        }
        if (Date.now() - (parsed.ts ?? 0) > RETRY_TTL_MS) {
            await clearRetryState(sessionKey, toolSlug);
            return null;
        }
        return parsed;
    }
    catch {
        return null;
    }
}

async function writeRetryState(sessionKey, toolSlug, patch) {
    try {
        await fs.mkdir(RETRY_DIR, { recursive: true });
        const current = await readRetryState(sessionKey, toolSlug) ?? {};
        const next = { ...current, ...patch, ts: Date.now() };
        await fs.writeFile(retryStatePath(sessionKey, toolSlug), JSON.stringify(next), "utf8");
    }
    catch {
        // Best-effort: write failure means this attempt is not counted toward the retry budget.
    }
}

async function clearRetryState(sessionKey, toolSlug) {
    try {
        await fs.unlink(retryStatePath(sessionKey, toolSlug));
    }
    catch {
        // File may not exist — ignore.
    }
}

function getRetryThresholds() {
    // GGUF models correct themselves poorly from feedback — escalate sooner.
    return IS_LOCAL_GGUF
        ? { feedbackAt: 2, capAt: 4 }
        : { feedbackAt: 3, capAt: 5 };
}

function buildFeedbackMessage(toolName, attempts, capAt, lastError, schema) {
    const lines = [
        `Tool call rejected (attempt ${String(attempts)} of ${String(capAt)}) — ${toolName}`,
        "",
        "Error returned:",
        `  ${lastError}`,
        "",
    ];
    const props = schema?.properties && typeof schema.properties === "object" ? schema.properties : {};
    const required = Array.isArray(schema?.required) ? schema.required : [];
    if (Object.keys(props).length > 0) {
        lines.push("Expected argument types:");
        for (const [name, spec] of Object.entries(props)) {
            const req = required.includes(name) ? " (required)" : "";
            const t = (spec && typeof spec === "object" && !Array.isArray(spec)) ? (spec.type ?? "any") : "any";
            lines.push(`  ${name}${req}: ${t}`);
        }
        lines.push("");
    }
    lines.push("Stop retrying with the same arguments. Fix the listed fields and try once more.");
    return lines.join("\n");
}

function buildCapMessage(toolName, attempts) {
    return [
        `Maximum retries reached for ${toolName} (attempt ${String(attempts)}).`,
        "Do not retry this tool call.",
        "Summarize what you tried to do and ask the user how to proceed.",
    ].join("\n");
}

function looksLikeMediaSongGoal(text) {
    const lower = text.toLowerCase();
    return (lower.includes("comfyui") && (lower.includes("song") || lower.includes("music") || lower.includes("audio")))
        || lower.includes("irish folk song")
        || lower.includes("pub stuffff");
}

function extractWorkflowPathFromText(text) {
    const match = text.match(/\/[^\s"'`]*ace_step_song_workflow\.json/);
    return match?.[0] ?? "";
}

function summarizeToolResult(toolName, text, current = {}) {
    const lower = text.toLowerCase();
    const patch = {
        lastTool: trimText(toolName, 120),
        lastToolAt: new Date().toISOString(),
    };
    if (toolName === "read") {
        const workflowPath = extractWorkflowPathFromText(text) || current.workflowPath;
        if (workflowPath) {
            patch.workflowPath = workflowPath;
            patch.phase = "workflow ready";
        }
        if (lower.includes("\"saveaudiomp3\"") || lower.includes("\"textencodeacestepaudio1.5\"")) {
            patch.workflowSummary = "ACE-Step workflow file exists with text encoder, sampler, audio decode, and MP3 save nodes.";
            patch.generationStarted = current.generationStarted === true;
            if (current.generationStarted !== true) {
                patch.blocker = "Workflow exists, but there is no confirmed ComfyUI prompt submission yet.";
            }
        }
    }
    if (toolName.includes("download") || lower.includes("downloading ") || lower.includes("huggingface.co")) {
        patch.phase = "model download";
    }
    if (lower.includes("\"prompt_id\"")) {
        patch.generationStarted = true;
        patch.phase = "generation submitted";
        patch.blocker = "";
        const match = text.match(/"prompt_id"\s*:\s*"([^"]+)"/);
        if (match?.[1]) {
            patch.promptId = match[1];
        }
    }
    if (lower.includes("still running after 30s")) {
        patch.generationStarted = true;
        patch.phase = "generation running";
        patch.blocker = "";
    }
    if (lower.includes("enoent") && lower.includes("ace_step_song_workflow.json")) {
        patch.phase = "workflow fetch";
        patch.blocker = "Workflow file was read before it existed.";
        patch.workflowPath = extractWorkflowPathFromText(text) || current.workflowPath;
    }
    return patch;
}

function isContaminatedCompaction(summary, goal) {
    const lowerSummary = summary.toLowerCase();
    const lowerGoal = goal.toLowerCase();
    if (!(lowerGoal.includes("song") || lowerGoal.includes("music") || lowerGoal.includes("comfyui"))) {
        return false;
    }
    return CONTAMINATION_MARKERS.some((marker) => lowerSummary.includes(marker));
}

function buildSessionStatusContext(state) {
    if (!state || typeof state !== "object" || Object.keys(state).length === 0) {
        return "";
    }
    const lines = [
        "## Structured Session State",
        "Treat this structured state as higher priority than compacted transcript prose when they conflict.",
    ];
    if (state.goal) {
        lines.push(`- Goal: ${state.goal}`);
    }
    if (state.phase) {
        lines.push(`- Phase: ${state.phase}`);
    }
    if (state.workflowPath) {
        lines.push(`- Workflow path: ${state.workflowPath}`);
    }
    if (state.workflowSummary) {
        lines.push(`- Workflow summary: ${state.workflowSummary}`);
    }
    if (state.lastTool) {
        lines.push(`- Last tool: ${state.lastTool}`);
    }
    if (state.generationStarted === true) {
        lines.push("- Generation started: yes");
    }
    else if (state.generationStarted === false) {
        lines.push("- Generation started: no");
    }
    if (state.lastOutputPath) {
        lines.push(`- Latest output path: ${state.lastOutputPath}`);
    }
    if (state.blocker) {
        lines.push(`- Blocker: ${state.blocker}`);
    }
    if (state.degradedAfterCompaction) {
        lines.push("- Warning: the last compaction summary was contaminated by unrelated task content; do not trust compacted prose over this state.");
    }
    return lines.join("\n");
}

async function readRecentTranscriptEntries(sessionId, limit = 160) {
    const trimmedId = typeof sessionId === "string" ? sessionId.trim() : "";
    if (!trimmedId) {
        return [];
    }
    try {
        const raw = await fs.readFile(path.join(SESSION_TRANSCRIPT_DIR, `${trimmedId}.jsonl`), "utf8");
        return raw
            .split(/\r?\n/)
            .filter(Boolean)
            .slice(-limit)
            .map((line) => {
            try {
                return JSON.parse(line);
            }
            catch {
                return null;
            }
        })
            .filter((entry) => entry && typeof entry === "object");
    }
    catch {
        return [];
    }
}

function deriveSessionStateFromTranscript(entries) {
    const state = {};
    let latestUserText = "";
    let lastAssistantText = "";
    let lastAssistantWasEmpty = false;
    let lastToolResultText = "";
    let lastToolName = "";
    for (const entry of entries) {
        if (!entry || typeof entry !== "object") {
            continue;
        }
        if (entry.type === "compaction") {
            const summary = trimText(entry.summary ?? "", 8000);
            if (summary && isContaminatedCompaction(summary, state.goal ?? latestUserText ?? "")) {
                state.degradedAfterCompaction = true;
                state.compactionWarning = trimText(summary, 500);
                if (!state.blocker) {
                    state.blocker = "Compaction summary included unrelated prior-task content.";
                }
            }
            const readFileMatch = summary.match(/<read-files>\s*([\s\S]*?)\s*<\/read-files>/i);
            if (readFileMatch?.[1]) {
                const workflowPath = extractWorkflowPathFromText(readFileMatch[1]);
                if (workflowPath) {
                    state.workflowPath = workflowPath;
                }
            }
            continue;
        }
        if (entry.type !== "message" || !entry.message || typeof entry.message !== "object") {
            continue;
        }
        const role = entry.message.role;
        const text = trimText(extractText(entry.message.content ?? entry.message), 12000);
        if (role === "user") {
            if (text) {
                latestUserText = text;
            }
            if (looksLikeMediaSongGoal(text)) {
                state.goal = trimText(text, 600);
                if (!state.phase) {
                    state.phase = "planning workflow";
                }
                if (typeof state.generationStarted !== "boolean") {
                    state.generationStarted = false;
                }
            }
            continue;
        }
        if (role === "toolResult") {
            const toolName = trimText(entry.message.toolName ?? "", 120);
            if (toolName) {
                lastToolName = toolName;
            }
            if (text) {
                lastToolResultText = text;
            }
            Object.assign(state, summarizeToolResult(toolName, text, state));
            const lower = text.toLowerCase();
            if (lower.includes("invalid json in workflow_json")) {
                state.phase = "workflow validation";
                state.blocker = "Workflow save failed because the workflow JSON sent to the gateway was invalid.";
            }
            if (lower.includes("pip: not found")) {
                state.phase = "environment setup";
                state.blocker = "The gateway container tried to install dependencies with pip, but pip is unavailable there.";
            }
            if (lower.includes("\"saveaudiomp3\"") || lower.includes("\"textencodeacestepaudio1.5\"")) {
                state.workflowSummary = "ACE-Step workflow graph is present in session history.";
            }
            continue;
        }
        if (role === "assistant") {
            if (text) {
                lastAssistantText = text;
                lastAssistantWasEmpty = false;
            }
            else {
                lastAssistantWasEmpty = true;
            }
        }
    }
    if (!state.phase && state.workflowPath) {
        state.phase = "workflow prepared";
    }
    return { state, latestUserText, lastAssistantText, lastAssistantWasEmpty, lastToolName, lastToolResultText };
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
let IS_LOCAL_GGUF = false;

// ---------------------------------------------------------------------------
// Response truncation — applied to all flat tool results before returning
// ---------------------------------------------------------------------------
const RESPONSE_CAP_CLOUD = 4000;
const RESPONSE_CAP_GGUF = 2000;
const SEARCH_MAX_ITEMS = 3;
const SEARCH_DESC_CLOUD = 200;
const SEARCH_DESC_GGUF = 150;

function isSearchTool(toolName) {
    const lower = toolName.toLowerCase();
    return (
        lower.includes("search") ||
        lower.includes("duckduckgo") ||
        lower.includes("tavily") ||
        (lower.includes("n8n") && (lower.includes("list") || lower.includes("search")))
    );
}

function truncateToolResult(text, toolName) {
    if (typeof text !== "string") {
        return String(text ?? "");
    }
    const cap = IS_LOCAL_GGUF ? RESPONSE_CAP_GGUF : RESPONSE_CAP_CLOUD;
    const descLen = IS_LOCAL_GGUF ? SEARCH_DESC_GGUF : SEARCH_DESC_CLOUD;
    let parsed;
    try {
        parsed = JSON.parse(text);
    }
    catch {
        // Not JSON — apply global cap only
        if (text.length > cap) {
            const sliceAt = cap - 40;
            return `${text.slice(0, sliceAt)}\u2026[${text.length - sliceAt} chars omitted]`;
        }
        return text;
    }
    // list_workflows: filter to mcp-api/* only
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && Array.isArray(parsed.workflow_files)) {
        const runnable = parsed.workflow_files.filter((w) => typeof w?.id === "string" && w.id.startsWith("mcp-api/"));
        const omitted = parsed.workflow_files.length - runnable.length;
        const out = { ...parsed, workflow_files: runnable };
        if (omitted > 0) {
            out._note = `${String(omitted)} non-runnable workflow files omitted (use workflow_id from mcp-api/* only)`;
        }
        text = JSON.stringify(out, null, 2);
    }
    // Search tools: cap results and truncate descriptions
    else if (isSearchTool(toolName) && parsed !== null) {
        const truncateDesc = (item) => {
            if (!item || typeof item !== "object" || Array.isArray(item)) {
                return item;
            }
            const out = { ...item };
            for (const field of ["description", "snippet", "text", "content", "body", "summary"]) {
                if (typeof out[field] === "string" && out[field].length > descLen) {
                    out[field] = `${out[field].slice(0, descLen)}\u2026`;
                }
            }
            return out;
        };
        const truncateList = (arr) => {
            if (!Array.isArray(arr)) {
                return arr;
            }
            const limited = arr.slice(0, SEARCH_MAX_ITEMS).map(truncateDesc);
            if (arr.length > SEARCH_MAX_ITEMS) {
                limited.push({ _note: `${String(arr.length - SEARCH_MAX_ITEMS)} more results omitted` });
            }
            return limited;
        };
        if (Array.isArray(parsed)) {
            parsed = truncateList(parsed);
        } else if (parsed && typeof parsed === "object") {
            for (const key of ["results", "items", "hits", "data"]) {
                if (Array.isArray(parsed[key])) {
                    parsed[key] = truncateList(parsed[key]);
                    break;
                }
            }
        }
        text = JSON.stringify(parsed, null, 2);
    }
    // Global cap
    if (text.length > cap) {
        const sliceAt = cap - 40;
        text = `${text.slice(0, sliceAt)}\u2026[${text.length - sliceAt} chars omitted]`;
    }
    return text;
}

function register(api) {
    const config = api.pluginConfig;
    if (!config?.servers || Object.keys(config.servers).length === 0) {
        return;
    }

    // Model-tier detection: explicit config overrides env-var auto-detection.
    if (config?.ggufMode === true) {
        IS_LOCAL_GGUF = true;
    } else if (config?.ggufMode !== false) {
        // Auto-detect from active model string
        const activeModel = (
            process.env.OPENCLAW_MODEL ??
            process.env.OPENCLAW_DEFAULT_MODEL ??
            ""
        ).toLowerCase();
        if (/\.gguf/i.test(activeModel) || /q[45678]_/i.test(activeModel) || activeModel.includes("gguf")) {
            IS_LOCAL_GGUF = true;
        }
    }
    if (IS_LOCAL_GGUF) {
        api.logger.info("[mcp-bridge] GGUF mode active — lower retry thresholds and tighter response caps enabled");
    }

    const flatToolsEnabled = config.flatTools === true;
    const injectSchemasEnabled = config.injectSchemas !== false;
    const mcpManager = new MCPManager(toManagerConfig(config));
    const latestUserMessages = new Map();
    let currentSessionKey = "";

    let connected = false;
    let connectingPromise = null;
    const ensureConnected = async () => {
        if (connected) {
            return;
        }
        if (connectingPromise !== null) {
            return connectingPromise;
        }
        connectingPromise = mcpManager.connectAll().then(() => {
            connected = true;
        }).finally(() => {
            connectingPromise = null;
        });
        return connectingPromise;
    };

    for (const [serverName, serverConfig] of Object.entries(config.servers)) {
        if (serverConfig.enabled === false) {
            continue;
        }
        const prefix = serverConfig.toolPrefix ?? serverName;
        api.registerTool({
            name: `${prefix}__call`,
            label: `MCP: ${serverName}`,
            description: `Legacy fallback for MCP server "${serverName}" (${serverConfig.url}). Prefer direct flat tools like ${prefix}__tool_name when available; use this only when a flat tool is unavailable or you must discover the raw MCP tool name first.`,
            parameters: Type.Object({
                tool: Type.Optional(Type.String({ description: `The raw MCP tool name to call on this server (e.g. "duckduckgo_web_search"). Run ${prefix}__discover first if unsure of the exact name.` })),
                toolName: Type.Optional(Type.String({ description: `Alias for tool. Accepted to recover from model formatting drift.` })),
                name: Type.Optional(Type.String({ description: `Alias for tool. Accepted to recover from model formatting drift.` })),
                namespacedTool: Type.Optional(Type.String({ description: `Alias for tool. Accepted to recover from model formatting drift.` })),
                args: Type.Optional(Type.Union([
                    Type.Record(Type.String(), Type.Unknown(), { description: "Arguments to pass to the tool" }),
                    Type.String({ description: "JSON object string of arguments; accepted for models that emit stringified tool args" }),
                ])),
            }),
            async execute(_toolCallId, params) {
                try {
                    await ensureConnected();
                    let toolName;
                    let args;
                    try {
                        toolName = coerceToolName(params, prefix);
                        args = coerceToolArgs(params.args);
                    }
                    catch (primaryErr) {
                        const recovered = recoverProxyInvocation(params);
                        if (!recovered) {
                            throw primaryErr;
                        }
                        toolName = coerceToolName({ tool: recovered.toolName }, prefix);
                        args = recovered.args;
                        api.logger.warn(`mcp-client: recovered malformed ${prefix}__call payload for tool "${toolName}"`);
                    }
                    const resolvedToolName = resolveProxyToolName(mcpManager, prefix, toolName);
                    if (resolvedToolName !== `${prefix}__${toolName}`) {
                        api.logger.info(`mcp-client: normalized proxy tool "${toolName}" -> "${resolvedToolName}"`);
                    }
                    // Retry budget: keyed on resolved tool name, not on gateway__call itself
                    const sessionKey = currentSessionKey;
                    const toolSlug = resolvedToolName.replace(/__/g, "_").slice(0, 60);
                    const thresholds = getRetryThresholds();
                    const retryState = await readRetryState(sessionKey, toolSlug);
                    const attempts = (retryState?.attempts ?? 0) + 1;
                    if (attempts > thresholds.capAt) {
                        await clearRetryState(sessionKey, toolSlug);
                        return {
                            content: [{ type: "text", text: buildCapMessage(resolvedToolName, attempts) }],
                            details: { server: serverName, tool: toolName, resolvedTool: resolvedToolName, capped: true },
                        };
                    }
                    const result = await mcpManager.callTool(resolvedToolName, args);
                    await clearRetryState(sessionKey, toolSlug);
                    const rawText = typeof result === "string" ? result : JSON.stringify(result, null, 2);
                    const text = truncateToolResult(rawText, resolvedToolName);
                    return {
                        content: [{ type: "text", text }],
                        details: { server: serverName, tool: toolName, resolvedTool: resolvedToolName, result },
                    };
                }
                catch (err) {
                    const message = err instanceof Error ? err.message : String(err);
                    const example = `Example: ${prefix}__call({"tool": "duckduckgo_web_search", "args": {"query": "your search"}})`;
                    if (message.includes("missing required tool name")) {
                        // Auto-discover: not a real tool failure, exempt from retry tracking
                        const discovered = mcpManager.getRegisteredTools()
                            .filter((t) => t.serverName === serverName)
                            .slice(0, 12)
                            .map((t) => compactToolMetadata(t, false));
                        const toolListText = discovered.length > 0
                            ? JSON.stringify({ server: serverName, count: discovered.length, tools: discovered }, null, 2)
                            : `No tools found on server "${serverName}".`;
                        return {
                            content: [{ type: "text", text: `You must provide a "tool" parameter. ${example}\n\nAvailable tools on "${serverName}":\n${toolListText}` }],
                            details: { server: serverName, autoDiscovered: true, count: discovered.length },
                        };
                    }
                    // Retry budget: record failure and inject feedback if threshold reached
                    // toolSlug and attempts may not be in scope here if resolvedToolName threw — guard with try
                    try {
                        const sessionKey = currentSessionKey;
                        const toolSlug = (toolName ?? "unknown").replace(/__/g, "_").slice(0, 60);
                        const retryState = await readRetryState(sessionKey, toolSlug);
                        const attempts = (retryState?.attempts ?? 0) + 1;
                        await writeRetryState(sessionKey, toolSlug, { attempts, lastError: message });
                        if (attempts >= getRetryThresholds().feedbackAt) {
                            const schema = mcpManager.getRegisteredTools().find((t) => t.namespacedName === `${prefix}__${toolName ?? ""}`)?.inputSchema;
                            return {
                                content: [{ type: "text", text: buildFeedbackMessage(`${prefix}__${toolName ?? "unknown"}`, attempts, getRetryThresholds().capAt, message, schema) }],
                                details: { server: serverName, tool: params?.tool ?? "<missing>", error: message, attempt: attempts },
                            };
                        }
                    }
                    catch {
                        // Best-effort retry tracking — swallow errors, fall through to original error response
                    }
                    const hint = message.includes("JSON parse failed")
                        ? `The "args" value must be valid JSON (use standard double quotes, not escape tokens). ${example}`
                        : message;
                    return {
                        content: [{ type: "text", text: `Error: ${hint}` }],
                        details: { server: serverName, tool: params?.tool ?? "<missing>", error: message },
                    };
                }
            },
        });
        api.logger.info(`mcp-client: registered proxy tool ${prefix}__call for server "${serverName}"`);
        api.registerTool({
            name: `${prefix}__discover`,
            label: `MCP Discover: ${serverName}`,
            description: `Discover tools on MCP server "${serverName}" with a compact result. Use this before ${prefix}__call when you do not know the exact tool name.`,
            parameters: Type.Object({
                query: Type.Optional(Type.String({ description: "Optional substring filter for tool name or description" })),
                limit: Type.Optional(Type.Number({ description: "Maximum number of tools to return", default: 12 })),
                includeSchema: Type.Optional(Type.Boolean({ description: "Include the full JSON schema for matching tools", default: false })),
            }),
            async execute(_toolCallId, params) {
                await ensureConnected();
                const query = typeof params.query === "string" ? params.query.trim().toLowerCase() : "";
                const limit = Number.isFinite(params.limit) ? Math.max(1, Math.min(50, Math.trunc(params.limit))) : 12;
                const includeSchema = params.includeSchema === true;
                const discovered = mcpManager.getRegisteredTools()
                    .filter((tool) => tool.serverName === serverName)
                    .filter((tool) => {
                    if (!query) {
                        return true;
                    }
                    const haystack = `${tool.originalName} ${tool.namespacedName} ${tool.description ?? ""}`.toLowerCase();
                    return haystack.includes(query);
                })
                    .slice(0, limit)
                    .map((tool) => compactToolMetadata(tool, includeSchema));
                const text = discovered.length > 0
                    ? JSON.stringify({ server: serverName, count: discovered.length, tools: discovered }, null, 2)
                    : JSON.stringify({
                        server: serverName,
                        count: 0,
                        tools: [],
                        hint: `No tools matched. Retry ${prefix}__discover with a broader query or omit query entirely.`,
                    }, null, 2);
                return {
                    content: [{ type: "text", text }],
                    details: { server: serverName, query, count: discovered.length, tools: discovered },
                };
            },
        });
        api.logger.info(`mcp-client: registered discovery tool ${prefix}__discover for server "${serverName}"`);
    }

    if (!flatToolsEnabled) {
        api.logger.info("[mcp-bridge] flatTools disabled - only gateway__call registered (set flatTools: true to enable eager discovery)");
    }

    if (flatToolsEnabled) {
        let flatToolsRegistered = false;
        let flatToolsRegistrationAttempts = 0;
        const MAX_FLAT_REGISTRATION_ATTEMPTS = 24;
        const registerFlatMcpTools = async () => {
            if (flatToolsRegistered) {
                return;
            }
            try {
                await ensureConnected();
                if (flatToolsRegistrationAttempts > 0) {
                    try {
                        await mcpManager.refreshTools();
                    }
                    catch (_refreshErr) {
                        // Refresh failed; fall through to check registry as-is.
                    }
                }
                const discovered = mcpManager.getRegisteredTools();
                if (discovered.length === 0) {
                    flatToolsRegistrationAttempts += 1;
                    if (flatToolsRegistrationAttempts >= MAX_FLAT_REGISTRATION_ATTEMPTS) {
                        api.logger.warn("[mcp-bridge] registerFlatMcpTools: giving up after " +
                            String(MAX_FLAT_REGISTRATION_ATTEMPTS) +
                            " attempts with 0 tools - check mcp-gateway and data/mcp/servers.txt (include comfyui). " +
                            "Use gateway__call with tool names from the gateway until flat tools appear.");
                        flatToolsRegistered = true;
                        return;
                    }
                    api.logger.warn("[mcp-bridge] registerFlatMcpTools: 0 tools in registry (attempt " +
                        String(flatToolsRegistrationAttempts) + "/" + String(MAX_FLAT_REGISTRATION_ATTEMPTS) +
                        "). Retrying in 5s - MCP gateway may still be loading servers.");
                    setTimeout(() => { registerFlatMcpTools().catch(() => { }); }, 5000);
                    return;
                }
                for (const rt of discovered) {
                    const srvCfg = config.servers[rt.serverName];
                    const pfx = srvCfg?.toolPrefix ?? rt.serverName;
                    api.registerTool({
                        name: rt.namespacedName,
                        label: `MCP ${rt.serverName}: ${rt.originalName}`,
                        description: `${rt.description ?? ""}\n\nSame as \`${pfx}__call\` with tool "${rt.originalName}" and args for parameters.`,
                        parameters: toFlatToolParametersSchema(rt),
                        async execute(_toolCallId, params) {
                            await ensureConnected();
                            const sessionKey = currentSessionKey;
                            const toolSlug = rt.namespacedName.replace(/__/g, "_").slice(0, 60);
                            const thresholds = getRetryThresholds();
                            const retryState = await readRetryState(sessionKey, toolSlug);
                            const attempts = (retryState?.attempts ?? 0) + 1;
                            // Tier 2: hard cap — stop before even calling the gateway
                            if (attempts > thresholds.capAt) {
                                await clearRetryState(sessionKey, toolSlug);
                                return {
                                    content: [{ type: "text", text: buildCapMessage(rt.namespacedName, attempts) }],
                                    details: { server: rt.serverName, tool: rt.originalName, capped: true },
                                };
                            }
                            try {
                                const coercedParams = coerceFlatToolParams(params, rt.inputSchema);
                                const result = await mcpManager.callTool(rt.namespacedName, coercedParams);
                                // Success: clear retry state
                                await clearRetryState(sessionKey, toolSlug);
                                const rawText = typeof result === "string" ? result : JSON.stringify(result, null, 2);
                                const text = truncateToolResult(rawText, rt.namespacedName);
                                return {
                                    content: [{ type: "text", text }],
                                    details: { server: rt.serverName, tool: rt.originalName, params: coercedParams, result },
                                };
                            }
                            catch (err) {
                                const message = err instanceof Error ? err.message : String(err);
                                await writeRetryState(sessionKey, toolSlug, { attempts, lastError: message });
                                // Tier 1: feedback injection
                                if (attempts >= thresholds.feedbackAt) {
                                    return {
                                        content: [{ type: "text", text: buildFeedbackMessage(rt.namespacedName, attempts, thresholds.capAt, message, rt.inputSchema) }],
                                        details: { server: rt.serverName, tool: rt.originalName, error: message, attempt: attempts },
                                    };
                                }
                                // Tier 0: pass error through normally (silent repair attempt)
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
        api.logger.info("[mcp-bridge] starting eager flat tool discovery");
        registerFlatMcpTools().then(() => {
            api.logger.info("[mcp-bridge] eager discovery done, registered=" + String(flatToolsRegistered));
        }).catch((err) => {
            api.logger.warn("[mcp-bridge] eager discovery error: " + String(err));
        });
    }

    api.registerHook("gateway_stop", async () => {
        if (connected) {
            await mcpManager.disconnectAll();
        }
    }, { name: "mcp-client-shutdown", description: "Disconnect all MCP servers" });

    api.registerHook("message_received", async (event) => {
        try {
            const { sessionId, sessionKey } = extractSessionIdentifiers(event);
            const text = trimText(extractText(event), 2000);
            if (!text) {
                return;
            }
            const key = safeStatusKey(sessionId, sessionKey);
            if (key) {
                latestUserMessages.set(key, text);
                currentSessionKey = key;
            }
            const patch = {};
            if (STATUS_REQUEST_RE.test(text)) {
                patch.lastStatusRequestAt = new Date().toISOString();
                patch.lastStatusRequestText = text;
            }
            else if (looksLikeMediaSongGoal(text)) {
                patch.goal = trimText(text, 600);
                patch.phase = "planning workflow";
                patch.generationStarted = false;
            }
            if (Object.keys(patch).length > 0) {
                await writeSessionStatus(sessionId, sessionKey, patch);
            }
        }
        catch (err) {
            api.logger.warn("[mcp-bridge] message_received status tracking failed: " + String(err));
        }
    }, { name: "mcp-session-status-received", description: "Track the latest user ask for status-safe replies" });

    api.registerHook("tool_result_persist", async (event) => {
        try {
            const { sessionId, sessionKey } = extractSessionIdentifiers(event);
            const toolName = extractToolName(event);
            if (!toolName) {
                return;
            }
            const text = trimText(extractText(event), 4000);
            const current = await readSessionStatus(sessionId, sessionKey);
            const patch = summarizeToolResult(toolName, text, current);
            if (Object.keys(patch).length > 0) {
                await writeSessionStatus(sessionId, sessionKey, patch);
            }
        }
        catch (err) {
            api.logger.warn("[mcp-bridge] tool_result_persist status tracking failed: " + String(err));
        }
    }, { name: "mcp-session-status-tools", description: "Persist compact task state from tool results" });

    api.registerHook("after_compaction", async (event) => {
        try {
            const { sessionId, sessionKey } = extractSessionIdentifiers(event);
            const summary = trimText(event?.summary ?? event?.data?.summary ?? extractText(event), 6000);
            if (!summary) {
                return;
            }
            const current = await readSessionStatus(sessionId, sessionKey);
            if (isContaminatedCompaction(summary, current.goal ?? "")) {
                await writeSessionStatus(sessionId, sessionKey, {
                    degradedAfterCompaction: true,
                    blocker: current.blocker || "Compaction summary included unrelated prior-task content; rely on structured session state instead.",
                    compactionWarning: trimText(summary, 500),
                });
            }
        }
        catch (err) {
            api.logger.warn("[mcp-bridge] after_compaction guard failed: " + String(err));
        }
    }, { name: "mcp-session-status-compaction", description: "Detect contaminated compactions for long-running sessions" });

    api.on("before_prompt_build", async (event) => {
        const firstServerName = Object.keys(config.servers)[0];
        const firstConfig = config.servers[firstServerName];
        const prefix = firstConfig?.toolPrefix ?? firstServerName;
        const contextLines = [
            "## MCP Tool Contract",
        ];
        if (flatToolsEnabled) {
            contextLines.push(`Prefer direct flat MCP tools like \`${prefix}__tool_name\` when they are available.`);
            contextLines.push(`Do not wrap a flat tool call inside \`${prefix}__call\`.`);
            contextLines.push(`Use \`${prefix}__call\` only as a legacy fallback when a flat tool is unavailable.`);
        }
        else {
            contextLines.push(`Use \`${prefix}__call\` to execute MCP tools.`);
        }
        contextLines.push(`Use \`${prefix}__discover\` first when you do not know the exact tool name or arguments.`);
        contextLines.push(`Inside \`${prefix}__call\`, pass the raw MCP tool name in \`tool\` without a \`${prefix}__\` prefix.`);
        contextLines.push(`Example: \`${prefix}__call({\"tool\":\"list_workflows\",\"args\":{\"details\":false}})\`.`);
        contextLines.push(`Names like \`${prefix}__search\` or \`${prefix}__run_workflow\` are tool names, not shell commands.`);
        contextLines.push(`Never run \`${prefix}__*\` through \`exec\`, shell, \`sh\`, or \`bash\`.`);
        contextLines.push("Never include raw `<|tool_call|>`, `<|tool_response|>`, `<channel|>`, or thought text inside tool arguments.");
        contextLines.push("Do not assume unavailable tools exist; discover them or report the failure truthfully.");
        const context = contextLines.join("\n");
        const { sessionId, sessionKey } = extractSessionIdentifiers(event);
        const transcriptEntries = await readRecentTranscriptEntries(sessionId);
        const transcriptState = deriveSessionStateFromTranscript(transcriptEntries);
        const statusState = await readSessionStatus(sessionId, sessionKey);
        const state = { ...statusState, ...transcriptState.state };
        const key = safeStatusKey(sessionId, sessionKey);
        const latestUserText = transcriptState.latestUserText || (key ? latestUserMessages.get(key) ?? "" : "");
        const additions = [context];
        const stateContext = buildSessionStatusContext(state);
        if (stateContext) {
            additions.push(stateContext);
        }
        const isStatusRequest = STATUS_REQUEST_RE.test(latestUserText);
        const isContinueRequest = CONTINUE_REQUEST_RE.test(latestUserText);
        if (state.degradedAfterCompaction) {
            additions.push([
                "## Compaction Recovery Rule",
                "A recent compaction summary contained unrelated task content.",
                "Trust the structured session state and the most recent local transcript over compacted prose when they conflict.",
                "Do not resume work on Telegram, Google Sheets, Suno, Runway, Creatomate, or n8n unless the current user explicitly asks for them.",
            ].join("\n"));
        }
        if (transcriptState.lastAssistantWasEmpty) {
            additions.push([
                "## Final Reply Guard",
                "The previous assistant turn ended with an empty message after tool activity.",
                "Do not end this turn with an empty assistant message.",
                "If you use tools, finish with at least one short non-empty assistant reply.",
                "Do not repeat the last raw tool payload verbatim.",
            ].join("\n"));
        }
        if (STATUS_REQUEST_RE.test(latestUserText)) {
            additions.push([
                "## Status Reply Rule",
                "The latest user asked for a status update.",
                "Reply in plain prose only, in 2-5 short sentences.",
                "Summarize current phase, last successful action, blocker, whether generation has started, and the latest output path if one exists.",
                "Do not call the read tool just to echo workflow JSON.",
                "Do not emit raw JSON, raw workflow objects, or an empty assistant message.",
            ].join("\n"));
        }
        else if (isContinueRequest) {
            additions.push([
                "## Continue Reply Rule",
                "The latest user asked you to continue the current task.",
                "Resume from the structured session state and recent transcript, not from contaminated compacted prose.",
                "Do not answer by dumping raw workflow JSON or repeating the last tool result.",
                "If you need to use the read tool, include an explicit absolute path.",
                "Keep any prose to one short sentence before taking the next concrete step.",
            ].join("\n"));
        }
        return { appendSystemContext: `\n\n${additions.filter(Boolean).join("\n\n")}` };
    }, { priority: 4 });

    if (injectSchemasEnabled) {
        let schemaContext = "";
        let schemaContextPromise = null;
        const loadSchemaContext = async () => {
            if (schemaContext) {
                return schemaContext;
            }
            if (schemaContextPromise !== null) {
                return schemaContextPromise;
            }
            schemaContextPromise = (async () => {
                await ensureConnected();
                const allSchemas = [];
                for (const [serverName, serverConfig] of Object.entries(config.servers)) {
                    if (serverConfig.enabled === false) {
                        continue;
                    }
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
                return buildSchemaContext(config, allSchemas, flatToolsEnabled);
            })().then((context) => {
                schemaContext = context;
                return context;
            }).finally(() => {
                schemaContextPromise = null;
            });
            return schemaContextPromise;
        };

        api.on("before_prompt_build", async () => {
            try {
                const context = await loadSchemaContext();
                if (context) {
                    return { appendSystemContext: context };
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
export { MCPManager } from "./manager/mcp-manager.js";
export { ToolRegistry } from "./manager/tool-registry.js";
export { StreamableHTTPTransport } from "./transport/streamable-http.js";
export { StdioTransport } from "./transport/stdio.js";
export { SSEParser, parseSSEStream } from "./transport/sse-parser.js";
export { configSchema } from "./config-schema.js";
export { MCPError } from "./types.js";
//# sourceMappingURL=index.js.map
