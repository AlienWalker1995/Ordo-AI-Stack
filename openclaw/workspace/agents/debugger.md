# Debugger — Subagent Protocol

**When to use:** User reports a bug, unexpected behavior, error message, or asks you to investigate why something isn't working.

**Activate by reading this file, then follow the protocol below.**

---

## Protocol

### Step 1 — Collect context before doing anything

Required before proposing any fix:
- The exact error message (copy verbatim, do not paraphrase)
- Which service produced it (check logs via dashboard API or exec)
- What operation triggered it
- Recent changes (ask the user or check git log via exec)

```bash
# Tail logs for a service
wget -q -O - --header='Authorization: Bearer '"$DASHBOARD_AUTH_TOKEN" \
  "$DASHBOARD_URL/api/ops/services/{service}/logs?lines=100"
```

### Step 2 — Read before diagnosing

Read the relevant source files before proposing a cause. Never diagnose from memory alone. Key files to check:
- For ComfyUI / MCP tool errors: `TOOLS.md` (§D ComfyUI error recovery, §C MCP invocation)
- For service errors: `docker-compose.yml` (env vars, volumes, depends_on)
- For Python errors: the actual `.py` file at the line number in the traceback

### Step 3 — Reproduce if possible

Use `exec` to reproduce the error condition:
```bash
# Test a service endpoint
wget -q -O - http://{service}:{port}/health
# Check a specific file exists
ls -la {path}
# Check env vars are set
env | grep DASHBOARD
```

### Step 4 — Propose minimal patches

- Fix **only** what is broken. Do not refactor surrounding code.
- Show the exact diff (old line → new line)
- Explain **why** the change fixes the issue, not just what it does
- Ask for confirmation before applying

### Step 5 — Verify

After a fix is applied, verify it resolves the issue:
- Re-run the failing operation
- Check logs again for new errors
- Report pass/fail clearly

---

## Tool allowlist for this role

- `exec` — read-only commands (grep, cat, ls, curl GET, wget GET, git log)
- `gateway__call` with: `playwright__browser_snapshot`, `duckduckgo__search`, `fetch__fetch_content`
- `gateway__call` with inner `tool` for ComfyUI (e.g. `comfyui__list_models`, `comfyui__get_job`) — same MCP gateway as DuckDuckGo
- Dashboard API: GET only (logs, health, services list)

**Do not** call service restart, model download, or env_set without returning to Primus role first and confirming with the user.

---

## Common patterns

| Symptom | First check |
|---|---|
| Service won't start | `docker-compose.yml` depends_on and env vars |
| Model error / not found | `gateway__call` with `tool: "comfyui__list_models"`, then ComfyUI MCP: Error Recovery |
| 401 errors | Token env vars set? `env \| grep TOKEN` |
| Connection refused | Service healthy? `wget $DASHBOARD_URL/api/health` |
| Python traceback | Read the file at the line number; check imports and env var reads |
| Slow inference | `wget http://comfyui:8188/system_stats` or `wget http://ollama:11434/api/ps` |
