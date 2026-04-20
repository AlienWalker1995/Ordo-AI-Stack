# Hermes Agent Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install NousResearch's hermes-agent as a host-mode assistant agent, wired to the stack's model-gateway and mcp-gateway over localhost. Phase 1 installs alongside OpenClaw (no OpenClaw changes).

**Architecture:** Hermes runs as a host Python 3.11 process in its own `uv`-managed venv at `vendor/hermes-agent/.venv/`. A new bootstrap script `scripts/start-hermes-host.sh` mirrors the existing `start-openclaw-host.sh` pattern: load `.env`, ensure `uv` + Hermes installed, `docker compose up -d`, wait for services, configure endpoints via `hermes config set`, then `exec hermes`.

**Tech Stack:** bash, Python 3.11, `uv` (astral.sh), docker-compose, pytest.

**Spec:** `docs/superpowers/specs/2026-04-18-hermes-agent-integration-design.md`.

---

### Task 1: Add Hermes paths to `.gitignore`

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Append Hermes ignore section**

Edit `.gitignore`, append these lines to the bottom of the file:

```
# Hermes Agent (host-mode install + local state; see docs/hermes-agent.md)
vendor/hermes-agent/
data/hermes/
```

- [ ] **Step 2: Verify no tracked files match the new patterns**

Run from repo root:

```bash
git ls-files vendor/hermes-agent/ data/hermes/
```

Expected output: empty (no tracked files should already match).

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore(gitignore): reserve vendor/hermes-agent and data/hermes"
```

---

### Task 2: Add Hermes section to `.env.example`

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Locate the OpenClaw block in `.env.example`**

Find the existing `OPENCLAW_*` section (near `OPENCLAW_CONTEXT_WINDOW`, `OPENCLAW_COMPACTION_MODE`, etc.). The Hermes block goes at the end of `.env.example`, as a new section after the last existing entry.

- [ ] **Step 2: Append Hermes section**

Append to the end of `.env.example`:

```
# --- Hermes Agent (phase-1 assistant agent evaluation) ---
# Hermes runs as a host process via scripts/start-hermes-host.sh (WSL2 or Git Bash).
# It points at model-gateway + mcp-gateway over localhost using the stack's existing
# LITELLM_MASTER_KEY, MODEL_GATEWAY_PORT, and MCP_GATEWAY_PORT. State lives in data/hermes/.
# See docs/hermes-agent.md for the validation checklist and known egress notes.
# HERMES_HOME overrides the default state dir (default: BASE_PATH/data/hermes).
# HERMES_HOME=/path/to/hermes/home
# Pin to a specific hermes-agent commit SHA. Filled by Task 3 of the integration plan.
# HERMES_PINNED_SHA=
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(env): add hermes-agent configuration section"
```

---

### Task 3: Clone hermes-agent and extract config keys

This task does no committing. Its output is a set of notes kept in your scratchpad that Task 5 consumes when writing the bootstrap script. The cloned repo itself is gitignored (Task 1).

**Files:**
- Create (gitignored): `vendor/hermes-agent/` (full repo clone)

- [ ] **Step 1: Clone hermes-agent**

From the repo root:

```bash
mkdir -p vendor
git clone https://github.com/NousResearch/hermes-agent.git vendor/hermes-agent
cd vendor/hermes-agent
```

- [ ] **Step 2: Pick and record a pinned commit SHA**

Pick the current `HEAD` of `main` (or the latest tagged release if one exists):

```bash
git log --oneline -1
# Record the SHA. Example output: "a1b2c3d feat: something"
# Copy the 40-char hash:
git rev-parse HEAD
```

Record this SHA as `HERMES_PINNED_SHA` in your scratchpad. You will paste it into `scripts/start-hermes-host.sh` (Task 5) and `.env.example` (optionally update the empty `HERMES_PINNED_SHA=` value in Task 2).

- [ ] **Step 3: Find the config key names**

Hermes reads config via `hermes config set <key> <value>`. We need four keys:

| Purpose | Script variable |
|---|---|
| OpenAI-compatible endpoint base URL | `HERMES_CFG_KEY_ENDPOINT` |
| OpenAI-compatible API key | `HERMES_CFG_KEY_APIKEY` |
| MCP server URL (streamable-http) | `HERMES_CFG_KEY_MCP_URL` |
| Honcho user modeling disable flag | `HERMES_CFG_KEY_HONCHO_DISABLE` |

From `vendor/hermes-agent/`, search:

```bash
# Config schema / defaults / CLI command
grep -rn "config" --include="*.py" -l | head -20
grep -rn "openai" --include="*.py" -l | head -20
grep -rn "mcp" --include="*.py" -l | head -20
grep -rn "honcho" --include="*.py" -l | head -20

# Inspect likely locations
find . -path ./node_modules -prune -o -name "config*.py" -print
find . -path ./node_modules -prune -o -name "settings*.py" -print
ls -la hermes/ 2>/dev/null || ls -la src/hermes/ 2>/dev/null
```

Look specifically for:
- A pydantic `BaseSettings` / `BaseModel` subclass defining the config schema
- A `[tool.hermes]` or similar section in `pyproject.toml`
- An example `config.toml` / `config.yaml` in `docs/` or `examples/`
- A CLI subcommand file (often `cli/config.py` or `commands/config.py`) that enumerates valid keys

Record the exact dotted key names (e.g. `model.openai.base_url`, `mcp.servers.gateway.url`) in your scratchpad. If Honcho has no disable flag, note that — the bootstrap will omit that line.

- [ ] **Step 4: Confirm MCP transport support**

Hermes's MCP client must support `streamable-http` to reach `http://localhost:8811/mcp`. Check:

```bash
grep -rn "streamable" --include="*.py" | head -20
grep -rn "StreamableHTTP\|streamable_http\|streamable-http" --include="*.py" | head -20
grep -rn "stdio\|transport" --include="*.py" | head -20
```

**Decision point:**
- If `streamable-http` is supported: continue with the plan as written. Record the transport key name if it's distinct from the URL (e.g. `mcp.servers.gateway.transport = "streamable-http"`).
- If only `stdio` is supported: the plan is still viable, but the bootstrap must launch MCP servers as local stdio subprocesses. Note this in your scratchpad and see Task 5 Step 3 fallback notes. **Do not proceed past this task without recording this decision.**

- [ ] **Step 5: Confirm Python 3.11 + `uv` install path works**

```bash
# If uv is not installed:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Then, from vendor/hermes-agent/:
uv venv --python 3.11
uv pip install -e ".[all]"
ls -la .venv/bin/hermes .venv/Scripts/hermes.exe 2>/dev/null
```

Expected: one of those two binaries exists. Record which path (`bin/hermes` on POSIX, `Scripts/hermes.exe` on Windows-native Python) applies to your host.

- [ ] **Step 6: Verify Hermes CLI starts and accepts `config set`**

```bash
./.venv/bin/hermes --version 2>&1 || ./.venv/Scripts/hermes.exe --version 2>&1
./.venv/bin/hermes config --help 2>&1 || ./.venv/Scripts/hermes.exe config --help 2>&1
```

Expected: CLI prints a version and the `config` subcommand help text. If either fails, stop and debug before proceeding to Task 4.

- [ ] **Step 7: Return to repo root — no commit**

```bash
cd ../../
# vendor/hermes-agent is gitignored; nothing to commit
git status --short
```

Expected: `git status` should not list `vendor/hermes-agent/` (it is ignored by Task 1's change).

---

### Task 4: Write the failing test for `scripts/start-hermes-host.sh`

**Files:**
- Create: `tests/test_start_hermes_host.py`

- [ ] **Step 1: Create the test file**

Write this content to `tests/test_start_hermes_host.py`:

```python
"""Static lint checks for scripts/start-hermes-host.sh.

These tests verify structural properties without running the script (no hermes
install required in CI). Manual smoke validation is documented in
docs/hermes-agent.md.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "start-hermes-host.sh"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def test_script_exists():
    assert SCRIPT.is_file(), f"{SCRIPT} missing"


def test_script_has_bash_shebang():
    first_line = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/usr/bin/env bash", f"unexpected shebang: {first_line!r}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_script_parses_as_bash():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_env_var_defaults_match_stack_conventions():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "${MODEL_GATEWAY_PORT:-11435}" in script
    assert "${MCP_GATEWAY_PORT:-8811}" in script
    assert "${LITELLM_MASTER_KEY:-local}" in script


def test_script_references_expected_paths():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "vendor/hermes-agent" in script
    assert "data/hermes" in script
    assert "docker compose up -d" in script


def test_script_stops_openclaw_defensively():
    """Operator runs Hermes OR OpenClaw — never both. Script stops any in-flight OpenClaw."""
    script = SCRIPT.read_text(encoding="utf-8")
    assert "openclaw-gateway" in script, "script should stop openclaw-gateway if running"


def test_env_example_has_hermes_section():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "Hermes Agent" in text
    assert "HERMES_HOME" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_start_hermes_host.py -v
```

Expected: 6 tests collected. `test_env_example_has_hermes_section` passes (Task 2 already added the section). The remaining 5 tests fail because `scripts/start-hermes-host.sh` does not exist yet. Record the failure output.

- [ ] **Step 3: Do not commit yet**

The test and the script it validates will be committed together in Task 5 Step 5 (standard TDD pairing).

---

### Task 5: Implement `scripts/start-hermes-host.sh`

**Files:**
- Create: `scripts/start-hermes-host.sh`

This step uses the values you recorded in Task 3. Wherever the script below references `<HERMES_CFG_KEY_*>` or `<PINNED_SHA>`, substitute the real value from your Task 3 scratchpad.

- [ ] **Step 1: Write the bootstrap script**

Write this content to `scripts/start-hermes-host.sh`. Replace every `<TASK_3:…>` marker with the value recorded in Task 3 before saving:

```bash
#!/usr/bin/env bash
# start-hermes-host.sh — Single-command bootstrap for host-mode Hermes Agent.
# Installs Hermes (if missing), starts Docker infra, launches Hermes CLI.
# Mirrors scripts/start-openclaw-host.sh; Hermes and OpenClaw must not run simultaneously.
set -eu
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── Pin ──
# Update deliberately; do not chase upstream main. See docs/hermes-agent.md for refresh cadence.
HERMES_REPO="https://github.com/NousResearch/hermes-agent.git"
HERMES_PINNED_SHA="${HERMES_PINNED_SHA:-<TASK_3:PINNED_SHA>}"
HERMES_DIR="$REPO_ROOT/vendor/hermes-agent"

# ── Phase 1: Load config ──
if [ -f .env ]; then
  set -a; source .env; set +a
fi

# ── Phase 2: Ensure uv ──
if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv (astral.sh)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "uv: $(uv --version 2>/dev/null || echo 'installed')"

# ── Phase 3: Clone Hermes if missing ──
if [ ! -d "$HERMES_DIR/.git" ]; then
  echo "==> Cloning hermes-agent..."
  mkdir -p "$(dirname "$HERMES_DIR")"
  git clone "$HERMES_REPO" "$HERMES_DIR"
fi
(cd "$HERMES_DIR" && git fetch --quiet origin && git checkout --quiet "$HERMES_PINNED_SHA")

# ── Phase 4: Install Hermes if venv missing ──
HERMES_BIN_POSIX="$HERMES_DIR/.venv/bin/hermes"
HERMES_BIN_WIN="$HERMES_DIR/.venv/Scripts/hermes.exe"
if [ ! -x "$HERMES_BIN_POSIX" ] && [ ! -x "$HERMES_BIN_WIN" ]; then
  echo "==> Installing hermes-agent into venv..."
  (cd "$HERMES_DIR" && uv venv --python 3.11 && uv pip install -e ".[all]")
fi
HERMES_BIN="$HERMES_BIN_POSIX"
[ -x "$HERMES_BIN_WIN" ] && HERMES_BIN="$HERMES_BIN_WIN"

# ── Phase 5: Start Docker infrastructure ──
echo "==> Starting Docker stack..."
docker compose up -d
# Defensive: Hermes and OpenClaw cannot share the model-gateway cleanly. Stop either in-flight.
docker compose stop openclaw-gateway openclaw-ui-proxy 2>/dev/null || true
pkill -f "openclaw gateway" 2>/dev/null || true

# ── Phase 6: Wait for services ──
echo "==> Waiting for services..."
until curl -sf "http://localhost:${MODEL_GATEWAY_PORT:-11435}/v1/models" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-local}" >/dev/null 2>&1; do sleep 3; done
echo "  model-gateway: OK"
until curl -sf http://localhost:8080/api/health >/dev/null 2>&1; do sleep 3; done
echo "  dashboard: OK"
until curl -sf "http://localhost:${MCP_GATEWAY_PORT:-8811}/health" >/dev/null 2>&1; do sleep 3; done
echo "  mcp-gateway: OK"

# ── Phase 7: Host-mode env vars ──
export HERMES_HOME="${HERMES_HOME:-$REPO_ROOT/data/hermes}"
mkdir -p "$HERMES_HOME"
export OPENAI_API_BASE="http://localhost:${MODEL_GATEWAY_PORT:-11435}/v1"
export OPENAI_API_KEY="${LITELLM_MASTER_KEY:-local}"

# ── Phase 8: Persist Hermes endpoint config ──
# Keys discovered in Task 3 investigation; substitute the values you recorded.
echo "==> Configuring Hermes endpoints..."
"$HERMES_BIN" config set <TASK_3:HERMES_CFG_KEY_ENDPOINT>      "$OPENAI_API_BASE"
"$HERMES_BIN" config set <TASK_3:HERMES_CFG_KEY_APIKEY>        "$OPENAI_API_KEY"
"$HERMES_BIN" config set <TASK_3:HERMES_CFG_KEY_MCP_URL>       "http://localhost:${MCP_GATEWAY_PORT:-8811}/mcp"
# If Task 3 found a Honcho disable key, uncomment; otherwise leave commented and document in docs/hermes-agent.md:
# "$HERMES_BIN" config set <TASK_3:HERMES_CFG_KEY_HONCHO_DISABLE> true

# ── Phase 9: Launch ──
cd "$REPO_ROOT"
echo "==> Launching Hermes CLI (HERMES_HOME=$HERMES_HOME)..."
exec "$HERMES_BIN"
```

**Fallback note for Task 3 Step 4:** If Hermes only supports `stdio` MCP transport (no streamable-http), replace Phase 8's `HERMES_CFG_KEY_MCP_URL` line with whatever Hermes's `stdio` MCP configuration uses (e.g. a command + args pointing at a local stdio-over-http adapter, or disabling MCP in phase 1 entirely and noting it in `docs/hermes-agent.md`). Do not leave a broken MCP config line in place.

- [ ] **Step 2: Verify every `<TASK_3:…>` marker was substituted**

```bash
grep -n "TASK_3:" scripts/start-hermes-host.sh
```

Expected output: empty (no markers remain). If any remain, go back to Task 3 and fill them in.

- [ ] **Step 3: Make the script executable (POSIX only)**

```bash
chmod +x scripts/start-hermes-host.sh
```

On Windows NTFS the executable bit is not preserved in git; this command is harmless.

- [ ] **Step 4: Run the tests and verify all pass**

```bash
pytest tests/test_start_hermes_host.py -v
```

Expected: all 6 tests pass.

- [ ] **Step 5: Commit the test and the script together**

```bash
git add tests/test_start_hermes_host.py scripts/start-hermes-host.sh
git commit -m "feat(hermes): add host-mode bootstrap script and static tests"
```

---

### Task 6: Write operator documentation

**Files:**
- Create: `docs/hermes-agent.md`

- [ ] **Step 1: Write the operator notes**

Write this content to `docs/hermes-agent.md`. Replace `<TASK_3:HONCHO_STATUS>` with one of: `"disabled via config"`, `"no disable flag found — known egress"`, or your Task 3 finding.

````markdown
# Hermes Agent (host-mode)

Phase-1 evaluation of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
as the stack's assistant-agent layer. Installed alongside OpenClaw; OpenClaw is not decommissioned
until phase 2.

## Why

OpenClaw has not reached a reliably working state on this stack. Hermes overlaps functionally
(messaging, MCP, cron, OpenAI-compatible) and adds a self-improving skill/learning system
(FTS5 session search, Honcho user modeling, autonomous skill creation from experience).

## Platform requirements

- **WSL2** (recommended) or **Git Bash** on Windows; Linux or macOS on POSIX hosts.
- Python **3.11** (installed automatically by `uv` into the Hermes venv — no system Python change).
- `uv` from astral.sh (installed automatically by the bootstrap if missing).
- The stack running (or startable via `docker compose up -d`).

## Running

From the repo root:

```bash
./scripts/start-hermes-host.sh
```

On first run this clones `vendor/hermes-agent/`, installs it into a dedicated venv, starts the
Docker stack, and launches the Hermes CLI. Subsequent runs skip the clone and install steps.

## Stopping

- `Ctrl-C` exits the Hermes CLI.
- The Docker stack keeps running. Stop it with `docker compose down` when desired.

## State

| Path | Contents |
|---|---|
| `vendor/hermes-agent/` | Upstream repo clone, pinned to a specific commit SHA |
| `vendor/hermes-agent/.venv/` | Python 3.11 venv managed by `uv` |
| `data/hermes/` | Hermes `HERMES_HOME` — config, skills, FTS5 sessions, Honcho state |

All three are gitignored. To fully reset:

```bash
rm -rf vendor/hermes-agent data/hermes
./scripts/start-hermes-host.sh
```

## Known egress

- **Honcho user modeling**: <TASK_3:HONCHO_STATUS>. If not disabled, Hermes will send conversation
  summaries to Honcho infrastructure for user modeling. Audit planned for phase 2.
- **`uv` install**: First run fetches `https://astral.sh/uv/install.sh` if `uv` is not already present.
  Install `uv` ahead of time (e.g. `winget install --id=astral-sh.uv -e`) if outbound access is
  blocked.
- **`hermes-agent` clone**: First run clones from GitHub. Pin to a specific SHA via
  `HERMES_PINNED_SHA` in `.env` to freeze upstream.

## Relationship to OpenClaw

Hermes and OpenClaw share the same model-gateway and cannot run simultaneously cleanly.
`scripts/start-hermes-host.sh` defensively stops any in-flight `openclaw-gateway` /
`openclaw-ui-proxy` containers and any host-mode `openclaw gateway` process before launching.

OpenClaw files, services, and `data/openclaw/` remain in the repo untouched during phase 1. They
are the safety net: if Hermes does not pan out, `./scripts/start-openclaw-host.sh` is still there.

Phase 2 (separate spec and plan) decommissions OpenClaw once Hermes is validated.

## Validation checklist

After `./scripts/start-hermes-host.sh`:

- [ ] Hermes CLI launches to its TUI.
- [ ] Hermes reports the local gateway model as available (slash-command or equivalent).
- [ ] Hermes MCP tool listing shows tools from mcp-gateway (ComfyUI, Tavily, n8n, GitHub,
      orchestration). If MCP support in Hermes is stdio-only, this may show empty — see Task 3
      notes in the implementation plan.
- [ ] Ask Hermes to read a repo file (e.g. `cat README.md`) — confirms host filesystem access.
- [ ] Ask Hermes to call Tavily search or a ComfyUI tool — confirms MCP roundtrip.
- [ ] Exit. Confirm `data/hermes/` now contains config/session files.

## Refreshing the pin

`HERMES_PINNED_SHA` is set at the top of `scripts/start-hermes-host.sh` (and optionally
overridden via `.env`). To upgrade:

```bash
cd vendor/hermes-agent
git fetch origin
git log --oneline origin/main -20
# pick a new SHA
```

Update `HERMES_PINNED_SHA` in the script (or `.env`), re-run the bootstrap. If the new version
changes config key names, re-run Task 3 of the integration plan to refresh the script's Phase 8.
````

- [ ] **Step 2: Commit**

```bash
git add docs/hermes-agent.md
git commit -m "docs(hermes): operator runbook and validation checklist"
```

---

### Task 7: Add README pointer

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Find the existing Docs line**

In `README.md`, locate the `**Docs:**` line (near the top, around line 23). It looks like:

```
**Docs:** [Getting started](docs/GETTING_STARTED.md) · [Configuration](docs/configuration.md) · ...
```

- [ ] **Step 2: Add a pointer to `docs/hermes-agent.md`**

Append ` · [Hermes Agent](docs/hermes-agent.md)` to the end of that line, just before the closing newline. Exact edit:

Find:
```
· [PRD](docs/Product%20Requirements%20Document.md)
```

Replace with:
```
· [PRD](docs/Product%20Requirements%20Document.md) · [Hermes Agent](docs/hermes-agent.md)
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): link hermes-agent runbook"
```

---

### Task 8: Manual smoke test (operator signoff)

No code changes. This is the end-to-end validation checklist. Run it and record the results.

- [ ] **Step 1: From a fresh shell (WSL2 or Git Bash), run the bootstrap**

```bash
cd /path/to/ordo-ai-stack
./scripts/start-hermes-host.sh
```

Expected output sequence:
- `==> Installing uv...` (first run only)
- `==> Cloning hermes-agent...` (first run only)
- `==> Installing hermes-agent into venv...` (first run only)
- `==> Starting Docker stack...`
- `  model-gateway: OK`
- `  dashboard: OK`
- `  mcp-gateway: OK`
- `==> Configuring Hermes endpoints...`
- `==> Launching Hermes CLI (HERMES_HOME=…/data/hermes)...`
- Hermes TUI renders.

If any `wait` step hangs beyond 2 minutes, interrupt and debug the service in question via
`docker compose ps` and `docker compose logs <service>`.

- [ ] **Step 2: Confirm model visibility**

Inside the Hermes TUI, ask it something model-bound (e.g. "what is 2+2?"). Expected: Hermes
responds using the local gateway model (check `model-gateway` logs for an inbound request:
`docker compose logs --tail=20 model-gateway`).

- [ ] **Step 3: Confirm MCP tool discovery**

Run Hermes's slash command to list MCP tools (exact command name recorded in your Task 3 notes).
Expected: at least one tool from ComfyUI / Tavily / n8n / GitHub / orchestration appears.

If MCP is empty, check:
- `docker compose logs --tail=50 mcp-gateway` for connection attempts from Hermes.
- Your Task 3 findings on MCP transport — if stdio-only, expected to be empty in phase 1.

- [ ] **Step 4: Confirm host filesystem access**

Ask Hermes: "Read and summarize the first 10 lines of `README.md` in this repo." Expected:
Hermes reads the file via its shell/file-read tooling and summarizes. This confirms the
"complete PC context" requirement.

- [ ] **Step 5: Confirm MCP roundtrip (if MCP wired)**

Ask Hermes to call a Tavily search ("search the web for 'nous research hermes agent release'")
or a ComfyUI workflow list. Expected: Hermes invokes the MCP tool and shows the result.

- [ ] **Step 6: Exit and confirm state persistence**

Exit the Hermes TUI. From repo root:

```bash
ls -la data/hermes/
```

Expected: config file(s) and possibly a `sessions.db` or similar — not empty.

- [ ] **Step 7: Record the result**

If all six steps pass: phase 1 is complete. Hermes is usable as the primary assistant.

If any step fails: open an issue or append to `docs/hermes-agent.md` under a new "Known issues"
section with the specific failure. Do not proceed to phase 2 (OpenClaw decommission) until
phase 1 is green.

- [ ] **Step 8: No commit** unless docs were updated

If Step 7 added a Known issues section to `docs/hermes-agent.md`:

```bash
git add docs/hermes-agent.md
git commit -m "docs(hermes): record phase-1 smoke findings"
```

Otherwise no changes to commit.
