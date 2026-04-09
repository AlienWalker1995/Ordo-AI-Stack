# MCP Context Management — Robust Tool-Call System

**Date:** 2026-04-08
**Status:** Approved, pending implementation
**Scope:** openclaw-mcp-bridge (`dist/index.js`), workspace bootstrap files, env/docker config

---

## Problem Statement

Local GGUF models (e.g. `gemma-4-31B-it-Q4_K_M.gguf`) running through OpenClaw exhibit three compounding failure modes:

1. **Tokenizer artifacts in tool args** — `<|"|>` and `<|'|>` tokens leak into JSON string values, causing integer and object fields to fail schema validation.
2. **Object-typed fields passed as malformed strings** — `overrides: "{height:1024"` instead of a proper object; existing coercion only handles scalar types, not objects.
3. **Unbounded retry loops** — the model repeats the same broken call 10+ times with zero adaptation; no mechanism caps retries or injects corrective feedback.

Secondary issues:
- **Context bloat** — `list_workflows` (60+ entries) and n8n template search results (multi-KB descriptions) dump into the model context unfiltered.
- **Bootstrap truncation** — HEARTBEAT.md 100% cut, TOOLS.md 24% cut at session start; critical operational knowledge lost before the first user message.

---

## Architecture

Six layers. Each has one job. Failures at any layer do not cascade.

```
OpenClaw (model generates tool call)
       │
       ▼
┌──────────────────────────────────────────┐
│ L5: Model-Tier Detection                 │  sets isLocalGGUF flag at registration
└────────────────┬─────────────────────────┘
                 │
                 ▼
┌──────────────────────────────────────────┐
│ L1: Enhanced Arg Coercion                │  sanitize → type-coerce → JSON-repair
│     object-string → coerceObjectField()  │  int artifact strip (e.g. "576]" → 576)
└────────────────┬─────────────────────────┘
                 │
                 ▼
┌──────────────────────────────────────────┐
│ L2: Retry Budget + Tiered Recovery       │  file-backed state, per session+tool
│     Tier 0 (1-2): silent repair          │
│     Tier 1 (3-4): feedback injection     │
│     Tier 2 (5+):  hard cap              │
└────────────────┬─────────────────────────┘
                 │
                 ▼
           MCP Gateway (mcp-gateway:8811)
                 │
                 ▼
┌──────────────────────────────────────────┐
│ L3: Response Truncation                  │  applied to all tool results before
│     4000 char global budget              │  returning to OpenClaw; list and
│     list_workflows: mcp-api/* only       │  search results capped per-type
│     search: 3 results, 200-char desc     │
└──────────────────────────────────────────┘

┌──────────────────────────────────────────┐
│ L4: Bootstrap Compression (workspace)    │  TOOLS.md ≤1400 chars, HEARTBEAT stub
│     Priority: AGENTS > SOUL > TOOLS > USER │  env vars set per-file + total budget
└──────────────────────────────────────────┘
```

All of Layers 1, 2, 3, and 5 live in `openclaw-mcp-bridge/dist/index.js`. Layer 4 is workspace file edits and `.env` tuning. No new services.

---

## Layer 1: Enhanced Arg Coercion

**File:** `openclaw/extensions/openclaw-mcp-bridge/dist/index.js`

### New function: `coerceObjectField(value)`

When `schema.type === "object"` and the incoming value is a string, run it through `coerceToolArgs()`. This reuses the existing JSON-repair pipeline (unquotes keys, repairs trailing brackets, handles `True`/`False`/`None`, etc.). Returns the repaired object on success, or the original value if all repair attempts fail.

```
coerceObjectField(value: unknown): object | unknown
  if typeof value !== "string" → return value as-is
  try coerceToolArgs(value) → return repaired object
  catch → return value (upstream validation will produce a clear error)
```

### Modification to `coerceFlatToolValue()`

Add an object branch before the current `return value` fallthrough:

```
if schema.type === "object" && typeof value === "string":
    return coerceObjectField(value)
```

### Integer trailing-artifact fix

In the existing integer coercion path, strip trailing `]`, `)`, `"`, and `'` before parseInt/parseFloat:

```
cleaned = sanitizeModelToolText(value).replace(/[,\])"']+$/, "").trim()
```

This handles Gemma patterns like `"576]"`, `"1024)"`, `"25"` (with extra quote).

---

## Layer 2: Retry Budget + Tiered Recovery

**File:** `openclaw/extensions/openclaw-mcp-bridge/dist/index.js`
**State dir:** `SESSION_STATUS_DIR/retry/` (already-defined constant in bridge)

### State file

Path: `SESSION_STATUS_DIR/retry/<safeKey>__<toolSlug>.json`

```json
{
  "attempts": 3,
  "tier": "feedback",
  "lastError": "overrides must be object",
  "ts": 1744160000
}
```

- `safeKey`: existing `safeStatusKey()` function applied to session ID
- `toolSlug`: tool name with `__` replaced by `_`, max 60 chars
- TTL: 30 minutes — state older than 30 min is ignored and deleted on next read

### Tier thresholds

| Mode       | Standard (cloud) | GGUF (local) |
|------------|-----------------|--------------|
| Repair     | attempts 1–2    | attempts 1–2 |
| Feedback   | attempts 3–4    | attempts 2–3 |
| Hard cap   | attempt 5+      | attempt 4+   |

GGUF mode lowers thresholds because local models are demonstrably worse at self-correcting from feedback.

### Tier 0 — Silent Repair

Coerce args, forward to MCP gateway. On success: delete state file, return result normally. On failure: increment `attempts`, write state file, return validation error.

### Tier 1 — Feedback Injection

Do not forward to MCP gateway. Return a structured error string written for the model:

```
Tool call rejected (attempt N of 4) — gateway__run_workflow

The following arguments were invalid:
  overrides: must be a JSON object, not a string — pass as {"key": value}
  width: must be an integer, not "576]" — pass as 576

Correct usage:
  prompt="your prompt here"
  width=576
  height=1024
  frames=121
  fps=24

Stop retrying with the same arguments. Fix the listed fields and try once more.
```

The error message is generated from the actual validation errors of the last failed attempt. Field-level hints are derived from the schema of the target tool.

### Tier 2 — Hard Cap

Return:
```
Maximum retries reached for gateway__run_workflow (attempt 5).
Do not retry this tool call. Summarize what you tried to do and ask the user how to proceed.
```

Delete state file. The model is instructed to surface the failure to the user rather than loop.

### Cleanup

TTL-only: on read, if `ts` is older than 30 minutes, treat the state as fresh — delete the file and start from attempt 1. No event-based cleanup is needed; the TTL handles session resets naturally (a new `/new` session starts after idle time and will always see expired state).

---

## Layer 3: Response Truncation

**File:** `openclaw/extensions/openclaw-mcp-bridge/dist/index.js`

New function: `truncateToolResult(result, toolName, isGGUF)` — applied at the end of every tool handler before returning to OpenClaw.

### list_workflows

Filter `workflow_files` array to entries whose `id` starts with `mcp-api/`. Append a note:
```
"... and 47 non-runnable workflow files omitted (use workflow_id from mcp-api/* only)"
```
`templates` array: include as-is (typically empty).

### Search tools

Pattern match on `toolName` for: `search`, `tavily`, `duckduckgo`, `n8n__search`, `n8n__list`.

- Cap results array at 3 items.
- Trim each item's `description` / `snippet` / `text` field to 200 chars.
- Append: `"... N more results omitted"` if truncated.

### Await / job status

No truncation — these responses are small and always fully needed.

### Global cap

After tool-specific truncation: convert result to JSON string. If length > 4000 chars (GGUF: 2000 chars), truncate to the cap and append `…[N chars omitted]`.

The GGUF cap is lower because the model's usable context window for active reasoning is smaller after bootstrap injection.

---

## Layer 4: Bootstrap Compression

### TOOLS.md

Target: ≤1400 chars (currently 3668 raw).

Remove:
- All code block examples (`{ "details": false }`, `gateway__call` fallback examples)
- The ComfyUI authoring flow narrative (keep only the pre-built workflow table)
- The ComfyUI model pull example block
- The "Full control checklist" numbered list

Keep:
- Pre-built runnable workflows table (`mcp-api/generate_video`, `generate_song`, `generate_image` with key inputs)
- Service URL table
- One-line tool usage rule: "Use flat `gateway__...` tools first; `gateway__call` only as fallback."
- Discord and web research one-liners

### HEARTBEAT.md

Currently injected as bootstrap but 100% cut when budget is exhausted. Convert to a tiny on-demand stub:

```markdown
# HEARTBEAT.md

Current health status is available on demand. Call gateway__get_services or
read agents/stack-ops.md for the runbook.
```

Actual heartbeat data (timestamps, model health) moves to a cron-injected system event that fires on session start — not bootstrap injection.

### AGENTS.md

Already structured with non-negotiables at the top. Verify total char count stays under `OPENCLAW_BOOTSTRAP_MAX_CHARS`. No content changes required.

### Env vars

Add to `.env`:

```env
OPENCLAW_BOOTSTRAP_MAX_CHARS=3000
OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS=12000
```

With TOOLS.md compressed to ≤1400 chars:
- AGENTS.md (~1800 chars) ✓
- SOUL.md (~800 chars) ✓
- TOOLS.md (~1400 chars) ✓
- USER.md (~1123 chars) ✓
- MEMORY.md (~200 chars) ✓
- Total: ~5300 chars — comfortably under 12000

---

## Layer 5: Model-Tier Detection

**File:** `openclaw/extensions/openclaw-mcp-bridge/dist/index.js`

### Detection

Read `process.env.OPENCLAW_MODEL` at plugin registration time. If not set, check `process.env.OPENCLAW_DEFAULT_MODEL`. 

GGUF detection (case-insensitive match on model string, evaluated in order):
1. Contains `.gguf` → definitive, stop
2. Contains standalone quantization suffix: `q4_`, `q5_`, `q6_`, `q8_` → definitive, stop
3. Contains bare `gguf` → definitive, stop
4. None of the above → not a GGUF model

The `gateway/` prefix alone is not a GGUF signal — cloud models routed through the model gateway also use that prefix.

Set module-level `let IS_LOCAL_GGUF: boolean = false` at module scope, assigned during `register()` call.

### Effect on other layers

| Setting              | Cloud model | Local GGUF  |
|----------------------|-------------|-------------|
| Object field repair  | On           | On (forced) |
| Int artifact strip   | On           | On (forced) |
| Feedback tier start  | Attempt 3   | Attempt 2   |
| Hard cap threshold   | Attempt 5   | Attempt 4   |
| Global response cap  | 4000 chars  | 2000 chars  |
| Search result cap    | 3 items     | 3 items     |
| Search desc length   | 200 chars   | 150 chars   |

---

## Error Handling

- **Retry state write failure**: log a warning, skip state persistence for this invocation — the tool call proceeds normally. No in-memory fallback; a write failure means this attempt is not counted toward the retry budget.
- **coerceObjectField repair failure**: return original value unchanged; the MCP gateway produces a clear validation error that feeds into the retry tier logic.
- **truncateToolResult error**: return the original result unchanged; never throw — truncation is best-effort.
- **Model detection failure**: default to cloud-model thresholds (conservative, less aggressive coercion).

---

## Testing

Existing test file: `tests/test_openclaw_mcp_bridge_contract.py`

New test cases to add:
1. Object-field coercion: `overrides: "{\"prompt\": \"test\"}"` → `overrides: {prompt: "test"}`
2. Integer trailing artifact: `width: "576]"` → `width: 576`
3. Retry tier 0 → increment on failure
4. Retry tier 1 → feedback message format, no gateway forward
5. Retry tier 2 → hard cap message, state file deleted
6. Retry state TTL expiry → treats as fresh attempt
7. list_workflows truncation → only mcp-api/* in output
8. Search truncation → max 3 results, 200-char desc
9. Global cap → result over 4000 chars gets truncated with annotation
10. GGUF detection → IS_LOCAL_GGUF=true for `.gguf` model strings

---

## Files Changed

| File | Change |
|------|--------|
| `openclaw/extensions/openclaw-mcp-bridge/dist/index.js` | L1, L2, L3, L5 implementation |
| `openclaw/workspace/TOOLS.md` | Compress to ≤1400 chars |
| `openclaw/workspace/HEARTBEAT.md` | Replace with 3-line stub |
| `.env` / `docker-compose.yml` | Add bootstrap char limit env vars |
| `tests/test_openclaw_mcp_bridge_contract.py` | 10 new test cases |
