# Remove OpenClaw Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surgically remove all OpenClaw-specific code, services, tests, dashboard UI, env vars, and ops-controller references. PRD and operator-doc cleanup is deferred.

**Architecture:** Ordered deletion — remove tests first (so they don't fail when code they test is gone), then dashboard routes, then UI + settings, then ops-controller, then Docker compose, then the `openclaw/` tree, then env vars, then docs. Verify after each step with `pytest`, `docker compose config`, and a targeted `rg` check.

**Tech Stack:** git, pytest, docker compose, Python 3.12, ripgrep.

**Spec:** `docs/superpowers/specs/2026-04-20-remove-openclaw-design.md`.

**Branch:** `feat/remove-openclaw` off `main` at `198f537`.

**Uncommitted operator state to preserve** (do NOT sweep into any commit):
- Modified: `.gitignore`, `tests/test_ops_controller_stats.py`
- Untracked: `ops-controller/entrypoint.sh`, `scripts/start-openclaw-host.sh`, `start-openclaw.ps1`, `test_output.log`

Always use targeted `git add <specific-path>` — never `git add .` or `git add -A`.

---

### Task 1: Delete OpenClaw-specific test files

**Files (delete):**
- `tests/test_openclaw_gateway_model_defaults.py`
- `tests/test_openclaw_mcp_bridge_contract.py`
- `tests/test_openclaw_mcp_bridge_runtime_contract.py`
- `tests/test_openclaw_mcp_plugin_config.py`
- `tests/test_validate_openclaw_config.py`
- `tests/test_merge_gateway_config.py`
- `tests/fixtures/openclaw_valid.json`

- [ ] **Step 1: Verify each file exists before deleting**

```bash
cd C:/dev/AI-toolkit
ls -1 tests/test_openclaw_gateway_model_defaults.py \
      tests/test_openclaw_mcp_bridge_contract.py \
      tests/test_openclaw_mcp_bridge_runtime_contract.py \
      tests/test_openclaw_mcp_plugin_config.py \
      tests/test_validate_openclaw_config.py \
      tests/test_merge_gateway_config.py \
      tests/fixtures/openclaw_valid.json
```

Expected: all seven paths print. If any is missing, note it — still attempt the rm below.

- [ ] **Step 2: Delete**

```bash
rm tests/test_openclaw_gateway_model_defaults.py \
   tests/test_openclaw_mcp_bridge_contract.py \
   tests/test_openclaw_mcp_bridge_runtime_contract.py \
   tests/test_openclaw_mcp_plugin_config.py \
   tests/test_validate_openclaw_config.py \
   tests/test_merge_gateway_config.py \
   tests/fixtures/openclaw_valid.json
```

- [ ] **Step 3: Run the remaining test suite to confirm no baseline regressions**

```bash
python -m pytest tests/ -q 2>&1 | tail -20
```

Expected: most tests still pass. Some may fail because they import from code that will be deleted later (dashboard, openclaw scripts). Record the failures — they'll be addressed in later tasks. **Do not fix them yet.**

- [ ] **Step 4: Commit**

```bash
git add tests/test_openclaw_gateway_model_defaults.py \
        tests/test_openclaw_mcp_bridge_contract.py \
        tests/test_openclaw_mcp_bridge_runtime_contract.py \
        tests/test_openclaw_mcp_plugin_config.py \
        tests/test_validate_openclaw_config.py \
        tests/test_merge_gateway_config.py \
        tests/fixtures/openclaw_valid.json
git diff --cached --stat      # verify: exactly 7 deletions, no other files
git commit -m "test: delete openclaw-specific test files"
```

---

### Task 2: Update dashboard tests that reference OpenClaw

**Files:**
- Inspect: `tests/test_dashboard_dependencies.py`
- Inspect: `tests/test_dashboard_service_pressure.py`
- Inspect: any other `tests/test_dashboard_*.py` that references openclaw

- [ ] **Step 1: Grep for openclaw in dashboard tests**

```bash
rg -i "openclaw" tests/test_dashboard_*.py
```

For each hit, decide:
- If the entire test is OpenClaw-specific (asserting on openclaw-gateway presence), delete the test function.
- If the test enumerates services/dependencies and incidentally includes openclaw, update the assertion to not expect openclaw.

- [ ] **Step 2: Edit each file**

Use Edit to remove OpenClaw-specific assertions. Preserve test function structure and other assertions. No wholesale rewrites.

- [ ] **Step 3: Run the affected tests**

```bash
python -m pytest tests/test_dashboard_dependencies.py tests/test_dashboard_service_pressure.py -v 2>&1 | tail -15
```

Expected: all still pass. If any fail because of a reference to `dashboard.app`-level OpenClaw symbols (which are still present at this point), those will resolve after Task 3. Record and proceed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_dashboard_dependencies.py tests/test_dashboard_service_pressure.py
# Include any other test files you edited
git diff --cached --stat
git commit -m "test(dashboard): drop openclaw assertions"
```

---

### Task 3: Remove dashboard backend routes and helpers

**Files:**
- Modify: `dashboard/app.py`

- [ ] **Step 1: Identify every OpenClaw reference**

```bash
rg -n "openclaw|OPENCLAW|_OPENCLAW" dashboard/app.py
```

Record every hit with its line number.

- [ ] **Step 2: Remove each reference with Edit tool**

Specifically remove:
- Any import/constant involving `OPENCLAW_CONFIG_PATH`.
- The `_ctx_raw` assignment that reads `OPENCLAW_CONTEXT_WINDOW` and the `OPENCLAW_CONTEXT_WINDOW` constant.
- The `openclaw_context_window` field in any system-info or health payload builder.
- Any code path that POSTs to `/services/openclaw-gateway/restart` after a model change (including the `openclaw_restarted` response field).
- The `_OPENCLAW_GATEWAY_BASE` constant.
- The `_make_openclaw_model()` helper function.
- Four route handlers:
  - `GET /api/openclaw/models`
  - `GET /api/openclaw/default-model`
  - `POST /api/openclaw/default-model`
  - `POST /api/openclaw/sync`
- Any imports from `dashboard.settings` that are only used by the above (check carefully — don't delete imports still used by retained code).

- [ ] **Step 3: Smoke check imports**

```bash
python -c "import dashboard.app" 2>&1 | tail -5
```

Expected: no output (successful import). If NameError, something referenced a removed symbol — fix it.

- [ ] **Step 4: Run dashboard tests**

```bash
python -m pytest tests/test_dashboard_*.py -q 2>&1 | tail -15
```

Expected: all pass.

- [ ] **Step 5: Verify no OpenClaw text remains in app.py**

```bash
rg -i "openclaw" dashboard/app.py
```

Expected: zero hits.

- [ ] **Step 6: Commit**

```bash
git add dashboard/app.py
git diff --cached --stat
git commit -m "feat(dashboard): remove openclaw backend routes and helpers"
```

---

### Task 4: Remove dashboard UI (HTML)

**Files:**
- Modify: `dashboard/static/index.html`

- [ ] **Step 1: Grep for openclaw in the HTML**

```bash
rg -in "openclaw" dashboard/static/index.html
```

Record every hit with line.

- [ ] **Step 2: Remove each UI element**

Specifically remove:
- The `.openclaw-sync-status` CSS class block (and any related styles).
- The "Sync to OpenClaw" button element, its container, and the status div it writes to.
- The emoji mapping entry for `openclaw` (likely a line like `openclaw: '🐾'` or similar in a JS object).
- The event listener that fetches `/api/openclaw/sync` and handles its response/toast.
- Any remaining `OpenClaw` / `openclaw` text (titles, tooltips, help text).

- [ ] **Step 3: Verify no OpenClaw text remains**

```bash
rg -i "openclaw" dashboard/static/index.html
```

Expected: zero hits.

- [ ] **Step 4: Commit**

```bash
git add dashboard/static/index.html
git diff --cached --stat
git commit -m "feat(dashboard): remove openclaw sync button, status div, emoji, CSS, and listener"
```

---

### Task 5: Remove dashboard settings, services catalog, dependency registry, entrypoint

**Files:**
- Modify: `dashboard/settings.py`
- Modify: `dashboard/services_catalog.py`
- Modify: `dashboard/dependency_registry.json`
- Modify: `dashboard/entrypoint.sh`

- [ ] **Step 1: `dashboard/settings.py` — strip OPENCLAW_* constants**

Use Edit to remove these constants and any related imports:
- `OPENCLAW_GATEWAY_PORT`
- `OPENCLAW_GATEWAY_INTERNAL_PORT`
- `OPENCLAW_UI_PORT`
- `OPENCLAW_GATEWAY_TOKEN`
- `OPENCLAW_CONFIG_PATH`

Verify: `rg -i "openclaw" dashboard/settings.py` returns zero.

- [ ] **Step 2: `dashboard/services_catalog.py` — strip the openclaw entry**

Remove:
- Any imports/reads of `OPENCLAW_GATEWAY_PORT`, `OPENCLAW_GATEWAY_INTERNAL_PORT`, `OPENCLAW_UI_PORT`, `OPENCLAW_GATEWAY_TOKEN` from `dashboard.settings`.
- The dict entry in `SERVICES` with `"id": "openclaw"` (about 6 lines).
- The `"openclaw": "openclaw-gateway"` entry in `OPS_SERVICE_MAP`.

Verify: `rg -i "openclaw" dashboard/services_catalog.py` returns zero.

- [ ] **Step 3: `dashboard/dependency_registry.json` — strip openclaw-gateway entry and critical flags**

Remove the JSON object whose key or `id` is `openclaw-gateway`. Also remove `"openclaw_critical": true` (or similar) from any other entries.

Verify:
```bash
python -c "import json; json.load(open('dashboard/dependency_registry.json'))"
rg -i "openclaw" dashboard/dependency_registry.json
```

Expected: JSON parses; grep returns zero hits.

- [ ] **Step 4: `dashboard/entrypoint.sh` — remove the openclaw-config permission-fix block**

Find and delete the block (about 5-7 lines) that chmods or chowns `/openclaw-config/openclaw.json` for appuser.

Verify: `rg -i "openclaw" dashboard/entrypoint.sh` returns zero.

- [ ] **Step 5: Run all dashboard tests**

```bash
python -m pytest tests/test_dashboard_*.py -q 2>&1 | tail -15
python -c "import dashboard.app" 2>&1
```

Expected: all pass; import succeeds.

- [ ] **Step 6: Commit**

```bash
git add dashboard/settings.py dashboard/services_catalog.py \
        dashboard/dependency_registry.json dashboard/entrypoint.sh
git diff --cached --stat
git commit -m "feat(dashboard): remove openclaw settings, catalog entry, dependency, and entrypoint fix"
```

---

### Task 6: Remove ops-controller allowlist entry

**Files:**
- Modify: `ops-controller/main.py`

- [ ] **Step 1: Grep for openclaw in ops-controller**

```bash
rg -in "openclaw" ops-controller/
```

Record all hits.

- [ ] **Step 2: Remove `"openclaw-gateway"` from `ALLOWED_SERVICES`**

Use Edit to remove the `"openclaw-gateway"` entry. Preserve formatting and comma handling.

Remove any other OpenClaw references found in Step 1.

- [ ] **Step 3: Verify import still works**

```bash
python -c "import ops_controller.main" 2>&1 | tail -3
# Or if importable as script:
python -m py_compile ops-controller/main.py
```

Expected: no error.

- [ ] **Step 4: Run ops-controller tests**

```bash
python -m pytest tests/test_ops_controller_stats.py -q 2>&1 | tail -10
```

Expected: pass (or at least: no new failures from our change — operator may have pre-existing uncommitted edits to that test file).

- [ ] **Step 5: Commit**

```bash
git add ops-controller/main.py
# Include any other ops-controller files that you edited. Do NOT add ops-controller/entrypoint.sh (untracked, operator's).
git diff --cached --stat
git commit -m "feat(ops-controller): drop openclaw-gateway from ALLOWED_SERVICES"
```

---

### Task 7: Remove Docker compose services, overrides, and named volume

**Files:**
- Modify: `docker-compose.yml`
- Delete: `overrides/openclaw-gateway-root.yml`
- Delete: `overrides/openclaw-secure.yml`

- [ ] **Step 1: Remove services from `docker-compose.yml`**

Use Edit to delete these service blocks (each is ~10-80 lines):
- `openclaw-workspace-sync`
- `openclaw-config-sync`
- `openclaw-plugin-install`
- `openclaw-plugin-config`
- `openclaw-gateway`
- `openclaw-ui-proxy`
- `openclaw-cli`
- `wait-orchestration`

After each deletion, check no other service has `depends_on: wait-orchestration` or `depends_on: openclaw-*`. If found, remove those dependency entries.

- [ ] **Step 2: Remove profiles**

Search for `profiles: [openclaw-docker]`, `profiles: [openclaw-setup]`, `profiles: [openclaw-cli]` references anywhere and remove them (they should only be inside deleted service blocks, but double-check).

- [ ] **Step 3: Remove the named volume**

Find the top-level `volumes:` mapping at the bottom of the file. Remove the `openclaw-extensions:` entry.

If the volumes section becomes empty after removal, either remove the `volumes:` key entirely or leave it as an empty mapping depending on what looks clean.

- [ ] **Step 4: Delete override files**

```bash
rm overrides/openclaw-gateway-root.yml
rm overrides/openclaw-secure.yml
```

- [ ] **Step 5: Validate compose**

```bash
docker compose config >/dev/null 2>&1
echo "exit=$?"
```

Expected: exit 0 (valid). If non-zero, run without redirect to see the error and fix.

- [ ] **Step 6: Confirm no OpenClaw in docker-compose.yml**

```bash
rg -i "openclaw" docker-compose.yml
```

Expected: zero hits.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml overrides/openclaw-gateway-root.yml overrides/openclaw-secure.yml
git diff --cached --stat
git commit -m "feat(compose): remove openclaw services, overrides, volume, and wait-orchestration"
```

---

### Task 8: Delete the `openclaw/` directory and OpenClaw scripts

**Files (delete):**
- Entire directory: `openclaw/`
- `scripts/fix_openclaw_workspace_permissions.ps1`
- `scripts/fix_openclaw_workspace_permissions.sh`
- `scripts/validate_openclaw_config.py`

Also delete the untracked `scripts/start-openclaw-host.sh` (it's not tracked; just `rm`).

- [ ] **Step 1: Delete**

```bash
cd C:/dev/AI-toolkit
rm -rf openclaw/
rm scripts/fix_openclaw_workspace_permissions.ps1
rm scripts/fix_openclaw_workspace_permissions.sh
rm scripts/validate_openclaw_config.py
rm -f scripts/start-openclaw-host.sh   # untracked; -f because it may or may not exist
```

- [ ] **Step 2: Verify**

```bash
ls openclaw/ 2>&1 | head -3         # expected: No such file or directory
ls scripts/fix_openclaw_workspace_permissions.* 2>&1 | head -3
ls scripts/validate_openclaw_config.py 2>&1 | head -3
ls scripts/start-openclaw-host.sh 2>&1 | head -3
```

All four should report missing.

- [ ] **Step 3: Stage the deletions**

```bash
git add -A openclaw/ scripts/fix_openclaw_workspace_permissions.ps1 \
           scripts/fix_openclaw_workspace_permissions.sh \
           scripts/validate_openclaw_config.py
git diff --cached --stat | head -20
```

Expected: many deletions from `openclaw/` (tracked files) plus 3 script deletions. Do NOT stage anything else.

- [ ] **Step 4: Smoke check**

```bash
rg -i "openclaw" scripts/ 2>&1 | head -5
```

Expected: zero hits.

- [ ] **Step 5: Commit**

```bash
git commit -m "chore: delete openclaw/ tree and openclaw-specific scripts"
```

---

### Task 9: Strip OPENCLAW_* and Discord/Telegram env vars from `.env.example`

**Files:**
- Modify: `.env.example`

**Note:** `.env.example` has uncommitted working-tree changes in the operator's other files — not this one. Still be targeted.

- [ ] **Step 1: Identify line ranges**

```bash
rg -n "OPENCLAW_|DISCORD_TOKEN|DISCORD_BOT_TOKEN|TELEGRAM_BOT_TOKEN" .env.example
```

Record line numbers.

- [ ] **Step 2: Remove lines and their explanatory comments**

For each variable listed in the spec's Section 3, remove the variable line. Also remove the 1-5 preceding comment lines that explain that variable (if those comments are unambiguously tied to this var — a "# OpenClaw session compaction policy..." comment immediately preceding `OPENCLAW_COMPACTION_MODE` is tied; a general "# --- Models ---" section header is not).

Use Edit tool with sufficient context to make each removal safe.

Leave intact:
- `BASE_PATH`, `DATA_PATH`, `LLAMACPP_*`, `MODEL_GATEWAY_*`, `MCP_GATEWAY_*`, `LITELLM_MASTER_KEY`, `HF_TOKEN`, `TAVILY_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`, `DASHBOARD_AUTH_TOKEN`, `OPS_CONTROLLER_TOKEN`, `HERMES_*`, everything Hermes-related, and any `COMFYUI_*`, `OPEN_WEBUI_*`, `N8N_*`, `QDRANT_*`, `RAG_*` vars.

- [ ] **Step 3: Verify**

```bash
rg -i "openclaw|DISCORD_TOKEN|DISCORD_BOT_TOKEN|TELEGRAM_BOT_TOKEN" .env.example
```

Expected: zero hits.

- [ ] **Step 4: Commit**

```bash
git add .env.example
git diff --cached --stat
git commit -m "docs(env): strip openclaw and orphaned discord/telegram vars"
```

---

### Task 10: Update `docs/hermes-agent.md` footnote

**Files:**
- Modify: `docs/hermes-agent.md`

- [ ] **Step 1: Replace the "Relationship to OpenClaw" section**

Find the section header `## Relationship to OpenClaw` and its body (approximately 6-8 lines).

Replace the entire section (header + body) with this single footer note at the end of the file (move it to the end; remove the original section from its original location):

```markdown
---

> **Note:** The stack previously used OpenClaw as its assistant-agent layer. It was decommissioned on 2026-04-20 — see `CHANGELOG.md` for the removal entry.
```

- [ ] **Step 2: Remove the "Running" section mention**

The "Running" section currently says the bootstrap "defensively stops any in-flight OpenClaw". Remove that sentence. If a paragraph collapses to nothing, remove the empty paragraph.

- [ ] **Step 3: Verify**

```bash
rg -i "openclaw" docs/hermes-agent.md
```

Expected: exactly one hit — the footnote.

- [ ] **Step 4: Commit**

```bash
git add docs/hermes-agent.md
git diff --cached --stat
git commit -m "docs(hermes): replace openclaw relationship section with decommission footnote"
```

---

### Task 11: Add CHANGELOG entry

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add entry under [Unreleased]**

Locate the `## [Unreleased]` heading (or the topmost section). Add this sub-section:

```markdown
### Removed
- OpenClaw assistant-agent layer — all services (`openclaw-gateway`, `openclaw-ui-proxy`, `openclaw-workspace-sync`, `openclaw-config-sync`, `openclaw-plugin-install`, `openclaw-plugin-config`, `openclaw-cli`), the `wait-orchestration` barrier, `overrides/openclaw-*.yml`, `openclaw/` tree, dashboard routes (`/api/openclaw/*`) and sync UI, ops-controller allowlist entry, env vars (`OPENCLAW_*`, orphaned `DISCORD_TOKEN`/`TELEGRAM_BOT_TOKEN`), and tests. Replaced by Hermes Agent (phase 1: 2026-04-18). Operator runtime data at `data/openclaw/` is left on disk — delete manually if desired.
```

Preserve the rest of the file exactly as-is.

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git diff --cached --stat
git commit -m "docs(changelog): record openclaw decommission"
```

---

### Task 12: Final verification sweep

No file changes unless stragglers are found.

- [ ] **Step 1: Run the verification grep**

```bash
rg -i openclaw \
  -g '!docs/**' \
  -g '!CHANGELOG.md' \
  -g '!vendor/**' \
  -g '!data/**'
```

Expected: zero hits.

- [ ] **Step 2: If any hits — fix and commit**

For each remaining hit, remove the reference via Edit. Commit with message `chore: remove straggler openclaw references`.

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest tests/ -q 2>&1 | tail -20
```

Expected: all pass.

- [ ] **Step 4: Validate compose**

```bash
docker compose config >/dev/null 2>&1 && echo OK || echo FAIL
```

Expected: `OK`.

- [ ] **Step 5: Import smoke checks**

```bash
python -c "import dashboard.app" 2>&1 | tail -3
python -m py_compile ops-controller/main.py && echo OK
```

Expected: no errors / `OK`.

- [ ] **Step 6: Report**

If all green, the removal is complete. Proceed to the code review task below.

---

### Task 13: Final code review

Dispatch the superpowers:code-reviewer subagent against the branch. Verify:
- All spec work buckets (1-9) implemented
- Verification grep passes
- Tests pass
- Compose parses
- No unrelated drive-by changes
- Commits are atomic and well-named

If the reviewer finds issues, fix and re-run.

---

### Task 14: Merge to main

Use the superpowers:finishing-a-development-branch skill.

- Verify tests pass.
- Present the 4 merge options to the user.
- If Option 1 (merge locally): switch to `main`, `git merge --no-ff feat/remove-openclaw`, delete the feat branch when the merge commit is on main.

No push to remote unless the user explicitly requests.
