# Agentic Design Patterns — insights & Ordo AI Stack mapping

This note **summarizes** themes and practical insights from *Agentic Design Patterns* by **Antonio Gulli** and **Mauro Sauco**, compiled by **Tom Mathews** in the open repository:

**[github.com/Mathews-Tom/Agentic-Design-Patterns](https://github.com/Mathews-Tom/Agentic-Design-Patterns)** (MIT License)

It is **not** a substitute for reading the book (≈424 pages, code examples, appendices). Use it to connect each part of the book to **OpenClaw**, **MCP**, and this **Ordo AI Stack** compose stack.

---

## What the book is trying to solve

From the upstream README, the book addresses questions production agents must answer:

- What if the agent **gets stuck** mid-task?
- How do you **preserve memory** across long sessions?
- How do you **prevent chaos** when running many agents?

It emphasizes **implementable** patterns: tool use, memory, exception logic, resource control, safety guardrails, multi-agent orchestration, and a **full chapter on MCP**.

---

## Introduction (repo folder `00-Introduction`)

| Piece | Insight | In this stack |
|-------|---------|----------------|
| **What makes a system an “agent”?** | Agency = goals, tools, environment feedback, not just a chat wrapper. | OpenClaw agents use **tools** (`gateway__call`, `message`, `read`, cron, etc.) against a **defined workspace** and **channels**. |
| **Power and responsibility** | Stronger autonomy ⇒ stronger need for boundaries and auditability. | **`SOUL.md`**, **`USER.md`**, **`OPENCLAW_UNRESTRICTED_GATEWAY_CONTAINER`** (opt-in), Discord allowlists, **TROUBLESHOOTING**. |
| **Foreword / dedication** | Positions the field: from demos to **reliable** systems. | Treat **cron**, **MCP health**, and **model ids** as production config — not “defaults” left undefined. |

---

## Part One — Foundational patterns (`01-Part_One`)

### Chapter 1 — Prompt chaining

- **Insight:** Break work into **ordered steps**; each step’s output feeds the next. Reduces single-shot overload and localizes failure.
- **Stack:** Long tasks → multiple **`exec`** / **`gateway__call`** rounds, or separate **cron** / **session** steps; document the chain in **`memory/YYYY-MM-DD.md`** or a runbook in **`workspace/agents/`**.

### Chapter 2 — Routing

- **Insight:** Classify **intent** or **input type** first, then dispatch to the right handler (model, tool set, or sub-workflow).
- **Stack:** Route “search” vs “image” vs “workflow” using **different MCP tools** (Tavily vs ComfyUI vs n8n); encode priorities in **`USER.md`** and **`TOOLS.md`**.

### Chapter 3 — Parallelization

- **Insight:** Independent subtasks can run **concurrently** when dependencies allow — cuts latency if you manage rate limits and ordering of merges.
- **Stack:** Multiple MCP calls **may** be parallel in principle; in practice this stack often **serializes** through one agent turn — use **n8n** or **dashboard** for true parallel pipelines when needed.

### Chapter 4 — Reflection

- **Insight:** Add a **critique / self-check** step: review draft output against criteria before finalizing.
- **Stack:** Second pass in same session, or **`USER.md`** checklist; for code, use **`read`** + **`edit`** with explicit verification steps.

### Chapter 5 — Tool use (function calling)

- **Insight:** Tools must have **clear schemas**, **discoverable names**, and **robust error surfaces**; the model should recover from tool errors without hallucinating success.
- **Stack:** Prefer **`gateway__call`** with explicit **`tool`** + **`args`** when flat tool names are missing or wrong. **Never** add a second **`gateway__`** after the OpenClaw plugin prefix (server name **`gateway`**). Validate tool names against the **Control UI** list.

### Chapter 6 — Planning

- **Insight:** Decompose goals into **plans** (steps, dependencies, stopping conditions). Re-plan when tools fail or the world changes.
- **Stack:** Put **planning rules** in **`AGENTS.md`** / role files under **`workspace/agents/`**; keep **cron** payloads short and require explicit **`message`** for Discord delivery.

### Chapter 7 — Multi-agent collaboration

- **Insight:** Split roles (researcher, planner, executor); define **handoff** and **shared state** to avoid drift.
- **Stack:** **`workspace/agents/<role>.md`** + separate sessions or Discord threads; **subagents** if your OpenClaw build supports them — align with **`sessions`** docs in OpenClaw.

---

## Part Two — Advanced systems (`02-Part_Two`)

### Chapter 8 — Memory management

- **Insight:** Separate **short-term** (context), **long-term** (persistent store), and **working** memory; **summarize** and **compress** before context limits bite.
- **Stack:** **`MEMORY.md`**, **`memory/YYYY-MM-DD.md`**, OpenClaw **`compaction`** in **`openclaw.json`**. Don’t rely on chat alone for durable state.

### Chapter 9 — Learning and adaptation

- **Insight:** Feedback loops (success/failure signals) improve behavior over time — **logging**, **evals**, **human corrections**.
- **Stack:** Discord delivery as ground truth; **dashboard** / **ops-controller** for infra; **merge_gateway_config** and **TROUBLESHOOTING** for iteration.

### Chapter 10 — Model Context Protocol (MCP)

- **Insight:** **Standard** way to expose tools to agents — **transport**, **discovery**, **schemas**; central gateway can aggregate many servers.
- **Stack:** Single URL **`http://mcp-gateway:8811/mcp`** via **openclaw-mcp-bridge**; **Tavily**, **DuckDuckGo**, **n8n**, **ComfyUI** in **`data/mcp/servers.txt`** + **`registry-custom.yaml`**. Set **`TAVILY_API_KEY`** for Tavily.

### Chapter 11 — Goal setting and monitoring

- **Insight:** Explicit **goals**, **metrics**, and **timeouts**; monitor progress and abort or escalate.
- **Stack:** **Cron** jobs with **`timeoutSeconds`**; **`payload.model`** must match a real **`gateway/…`** id; watch **`lastRunStatus`** / **Discord** for actual delivery.

---

## Part Three — Production concerns (`03-Part_Three`)

### Chapter 12 — Exception handling and recovery

- **Insight:** **Retry** with backoff, **idempotent** tools where possible, **graceful degradation** (fallback tool or partial answer).
- **Stack:** On tool failure (`Tool not found`, MCP errors), retry **`gateway__call`** with **exact** inner tool id; **web_fetch** only when appropriate; **never** claim success if the tool returned an error.

### Chapter 13 — Human in the loop

- **Insight:** **Approval gates** for sensitive actions; clear **escalation** when automation is uncertain.
- **Stack:** Confirm before destructive **`exec`**; use **Discord** for visibility; **elevated** / **exec** gated by **`tools.elevated`** and **`OPENCLAW_ELEVATED_ALLOW_WEBCHAT`** (see docs).

### Chapter 14 — Knowledge retrieval (RAG)

- **Insight:** **Retrieve** + **ground** answers in documents; manage chunking, embedding drift, and staleness.
- **Stack:** Optional **RAG profile** (`qdrant`, `rag-ingestion`) in compose; **Tavily** / **`web_fetch`** for live web; workspace **`.md`** files for local grounding.

---

## Part Four — Multi-agent architectures (`04-Part_Four`)

### Chapter 15 — Inter-agent communication (A2A)

- **Insight:** **Structured** messages between agents (tasks, not raw chat soup); **schemas** reduce misunderstanding.
- **Stack:** **Discord** / **channels** as human-visible bus; **MCP** as tool bus; avoid mixing **secrets** into untrusted tool outputs.

### Chapter 16 — Resource-aware optimization

- **Insight:** **Tokens**, **CPU**, **GPU**, **rate limits**, **cost** — schedule and batch work accordingly.
- **Stack:** **`OLLAMA_NUM_CTX`**, model-gateway **context**, **Tavily** rate limits; **ComfyUI** / **n8n** for heavy jobs off the gateway.

### Chapter 17 — Reasoning techniques

- **Insight:** **Chain-of-thought**, **self-consistency**, **tree-of-thoughts** — use when tasks need deliberation, not for every turn.
- **Stack:** Pick **reasoning models** via **gateway** (`gateway/…` ids); keep **cron** prompts simple if the model is slow.

### Chapter 18 — Guardrails and safety patterns

- **Insight:** **Input** validation, **output** filtering, **tool allowlists**, **SSRF** awareness (e.g. blocking internal URLs).
- **Stack:** **`tools.deny`** (e.g. **`browser`**), **`web_fetch`** private-URL blocks, **Discord** allowlists, **MCP** as controlled egress — see **OPENCLAW_SECURE.md** and **TROUBLESHOOTING**.

### Chapter 19 — Evaluation and monitoring

- **Insight:** **Metrics** beyond “vibes”: task success, latency, tool error rates, regressions.
- **Stack:** **Dashboard** health, **mcp-gateway** logs, **Discord** delivery confirmation, **cron** `lastError` fields.

### Chapter 20 — Prioritization

- **Insight:** When everything is urgent, **order** by impact and dependencies; **drop** or **defer** explicitly.
- **Stack:** **`USER.md`** ordering; **cron** schedules; **n8n** for queue-like workflows.

### Chapter 21 — Exploration and discovery

- **Insight:** **Balance** exploitation (known good tools) with **exploration** (trying new tools/paths) — with **safeguards**.
- **Stack:** Add MCP servers via **`dashboard`** / **`servers.txt`** with **review**; test in a **session** before automating in **cron**.

---

## Appendix (`05-Appendix`)

| Appendix | Insight | Stack hook |
|----------|---------|------------|
| **A — Advanced prompting** | Techniques for **reliability** (structure, constraints, few-shot within policy). | Put **critical** rules at the **top** of **`AGENTS.md`** (OpenClaw may truncate long files). |
| **B — GUI to real-world** | Agents move from **UI-only** to **environment** (tools, APIs, OS). | **`exec`** in gateway container, **MCP** to external services; know **Docker** limits. |
| **C — Agentic frameworks** | Landscape of frameworks — **interoperability** and **lock-in** tradeoffs. | This repo is **OpenClaw** + **MCP gateway** + **compose** — not LangChain/CrewAI, but **MCP** is the shared tool layer. |
| **D — AgentSpace** | Online-only vendor walkthrough (if present). | Optional external reference. |
| **E — Agents on the CLI** | CLI patterns for **scriptable** agents. | **`openclaw-cli`** profile in compose; **host** scripts under **`scripts/`**. |
| **F — Reasoning engines** | How models differ internally; **limits** of “reasoning” claims. | Route by **model id** (`gateway/…`); don’t over-trust **uncensored** or **reasoning** tags for safety. |
| **G — Coding agents** | Patterns for **code-focused** agents (review, test, patch). | **`apply_patch`**, **`edit`**, **`exec`** in workspace; CI **tests** in repo. |

---

## Ordo AI Stack / OpenClaw checklist (from the patterns above)

1. **One MCP URL** — `http://mcp-gateway:8811/mcp` in **openclaw-mcp-bridge** plugin.  
2. **Correct tool ids** — `gateway__<server>__<tool>` **once**; use **`gateway__call`** if unsure.  
3. **Secrets** — `TAVILY_API_KEY`, tokens in **`.env`**; not in workspace prompts.  
4. **Models** — **`gateway/<ollama-id>`** for cron; never **`default`** as a model name.  
5. **Memory** — **`MEMORY.md`** + compaction; don’t rely on chat alone.  
6. **Safety** — `tools.deny`, channel allowlists, unrestricted **exec** only when intended.  
7. **Truncation** — long **`AGENTS.md`** may be cut in bootstrap; keep **TOOLS** contract short.  
8. **Discord** — **`message`** with **`to: "channel:<id>"`** for delivery.

---

## Cron and scheduled agents (recap)

- **`payload.model`** in **`data/openclaw/cron/jobs.json`** must match a real **`gateway/…`** id (same family as **`agents.defaults.model.primary`** in **`openclaw.json`**).  
- **Discord:** require **`message`** with **`to: "channel:<snowflake>"`** — see **TROUBLESHOOTING** (cron + Discord).

---

## Further reading

- **Book repository:** [Mathews-Tom/Agentic-Design-Patterns](https://github.com/Mathews-Tom/Agentic-Design-Patterns)  
- **This stack:** **`TOOLS.md`**, **`AGENTS.md`**, **`docs/runbooks/TROUBLESHOOTING.md`**, **`mcp/README.md`**, **`openclaw/README.md`**
