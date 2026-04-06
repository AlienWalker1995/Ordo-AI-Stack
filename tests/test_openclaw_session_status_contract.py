from pathlib import Path


def test_mcp_bridge_persists_session_status_and_status_rule():
    source = Path("openclaw/extensions/openclaw-mcp-bridge/dist/index.js").read_text(encoding="utf-8")
    assert 'const SESSION_STATUS_DIR = path.join(OPENCLAW_HOME, "agents", "main", "session-status");' in source
    assert 'const SESSION_TRANSCRIPT_DIR = path.join(OPENCLAW_HOME, "agents", "main", "sessions");' in source
    assert 'api.registerHook("message_received"' in source
    assert 'api.registerHook("tool_result_persist"' in source
    assert 'api.registerHook("after_compaction"' in source
    assert '## Structured Session State' in source
    assert '## Status Reply Rule' in source
    assert '## Continue Reply Rule' in source
    assert '## Final Reply Guard' in source
    assert "Do not emit raw JSON, raw workflow objects, or an empty assistant message." in source


def test_workspace_agents_require_prose_status_reply():
    agents = Path("openclaw/workspace/AGENTS.md").read_text(encoding="utf-8")
    runtime_agents = Path("data/openclaw/workspace/AGENTS.md").read_text(encoding="utf-8")
    assert "Do not answer with raw workflow JSON, raw tool payloads, or an empty assistant message." in agents
    assert "Do not answer with raw workflow JSON, raw tool payloads, or an empty assistant message." in runtime_agents
    assert "Continue replies: if the user says `continue`, `resume`, `go on`, or similar, continue from the current task state." in agents
    assert "Continue replies: if the user says `continue`, `resume`, `go on`, or similar, continue from the current task state." in runtime_agents
