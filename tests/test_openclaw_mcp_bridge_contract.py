from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIST = REPO_ROOT / "openclaw" / "extensions" / "openclaw-mcp-bridge" / "dist" / "index.js"


def test_mcp_bridge_prompt_prefers_flat_tools_when_enabled():
    text = BRIDGE_DIST.read_text(encoding="utf-8")

    assert "Prefer direct flat MCP tools like" in text
    assert "Do not wrap a flat tool call inside" in text
    assert "only as a legacy fallback when a flat tool is unavailable" in text
    assert "Never run \\`${prefix}__*\\` through \\`exec\\`, shell, \\`sh\\`, or \\`bash\\`." in text
    assert "Never include raw `<|tool_call|>`, `<|tool_response|>`, `<channel|>`, or thought text inside tool arguments." in text


def test_mcp_bridge_recovers_malformed_gateway_call_payloads():
    text = BRIDGE_DIST.read_text(encoding="utf-8")

    assert "function recoverProxyInvocation(params)" in text
    assert "mcp-client: recovered malformed" in text
    assert '.replace(/\\\\"/g, \'"\')' in text
    assert 'extractBalancedObject(combined, /gateway__call\\s*\\(/i)' in text


def test_mcp_bridge_relaxes_and_coerces_flat_tool_params():
    text = BRIDGE_DIST.read_text(encoding="utf-8")

    assert "function buildLooseToolSchema(schema)" in text
    assert 'schema.type === "integer" || schema.type === "number" || schema.type === "boolean"' in text
    assert "function coerceFlatToolParams(params, schema)" in text
    assert "const coercedParams = coerceFlatToolParams(params, rt.inputSchema);" in text
    assert "params: coercedParams" in text


def test_mcp_bridge_loosens_object_type_schema():
    text = BRIDGE_DIST.read_text(encoding="utf-8")

    # String fallback added when anyOf contains an object variant
    assert "hasObjectType" in text
    assert "object-string fallback" in text
    # Direct object type also gets a string fallback
    assert "anyOf: [loosened, stringFallback]" in text


def test_mcp_bridge_coerces_object_string_fields():
    text = BRIDGE_DIST.read_text(encoding="utf-8")

    assert "function coerceObjectField(value)" in text
    assert "return coerceToolArgs(value)" in text
    # coerceFlatToolValue calls it for string values in object context
    assert "return coerceObjectField(value)" in text


def test_mcp_bridge_strips_integer_trailing_artifacts():
    text = BRIDGE_DIST.read_text(encoding="utf-8")

    # Regex that strips trailing ], ), ", ' from integer strings
    assert r'.replace(/[\])"\']+$/, "")' in text


def test_mcp_bridge_model_tier_detection():
    text = BRIDGE_DIST.read_text(encoding="utf-8")

    assert "let IS_LOCAL_GGUF = false;" in text
    assert "IS_LOCAL_GGUF = true;" in text
    # Detection checks
    assert r"/\.gguf/i" in text
    assert r"/q[45678]_/i" in text
    assert 'api.logger.info("[mcp-bridge] GGUF mode' in text
