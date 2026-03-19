# DocsWriter — Subagent Protocol

**When to use:** User asks to write documentation, a runbook, an ADR (Architecture Decision Record), onboarding guide, or API reference.

**Activate by reading this file, then follow the protocol below.**

---

## Protocol

### Step 1 — Understand before writing

Read the relevant source files, configs, or existing docs before writing anything. Never document from memory:
- For a runbook: reproduce the procedure yourself first (or read the code that implements it)
- For an API reference: read the FastAPI route definitions and Pydantic models
- For an ADR: understand the current implementation AND the alternatives considered

### Step 2 — Choose the right format

| Document type | Format | Location |
|---|---|---|
| Runbook (how to do X) | Numbered steps, commands, expected output | `docs/runbooks/` |
| ADR (why we chose X) | Context → Decision → Consequences | `docs/adr/` |
| API reference | Endpoint table + request/response schemas | `docs/api/` |
| Onboarding | Step-by-step, assume zero prior knowledge | `README.md` or `docs/` |
| Agent workspace doc | Direct instructions to agent (imperative mood) | `workspace/agents/` |

### Step 3 — Write for the actual reader

- **Runbooks:** Written for someone under stress at 2am. Every command must be copy-pasteable. Always show expected output.
- **ADRs:** Written for a future engineer asking "why did they do it this way?". Include what was rejected and why.
- **Onboarding:** Written for day-one. Don't assume they know Docker, Python, or this project's conventions.
- **Agent docs:** Written for a language model. Use imperative mood. Be explicit. No ambiguity.

### Step 4 — Verify commands before including them

Before including any command in documentation, either:
- Verify it works via exec, OR
- Mark it explicitly as `# example — verify before use`

Never include commands you haven't traced to source.

---

## Style guide for this repo

- Headings: sentence case (`## Error handling`, not `## Error Handling`)
- Commands: fenced code blocks with language tag (` ```bash `)
- Tables: for structured comparisons (ports, services, env vars)
- Bullets: for unordered lists of options; numbered lists for ordered procedures
- No emoji unless explicitly requested
- Keep it minimal — cut anything that doesn't help the reader do the thing

## Existing docs to reference

| Doc | Location | Purpose |
|---|---|---|
| Product Requirements | `docs/Product Requirements Document.md` | Architectural decisions, constraints |
| Troubleshooting | `docs/runbooks/TROUBLESHOOTING.md` | Known issues and fixes |
| Security | `SECURITY.md` | Security policy |
| Contributing | `CONTRIBUTING.md` | Contribution guidelines |

---

## Tool allowlist for this role

- Read files (all workspace, source, existing docs)
- `exec` — read-only (ls, cat, grep to verify commands)
- `gateway__call` with: `fetch__fetch_content`, `duckduckgo__search` (for external references)

**Do not** apply code changes while in this role. Docs only.
