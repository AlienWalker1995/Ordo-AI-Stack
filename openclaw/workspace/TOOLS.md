# TOOLS.md

Environment-specific notes for this AI-toolkit install.

## Services

| Service | URL (internal) | Purpose |
|---------|---------------|---------|
| **Model Gateway** | `http://model-gateway:11435/v1` | All LLM calls (OpenAI-compatible) |
| **Ollama** | `http://ollama:11434` | Direct Ollama API (prefer gateway) |
| **MCP Gateway** | `http://mcp-gateway:8811/mcp` | MCP tools (search, fetch, GitHub, etc.) |
| **Dashboard** | `http://localhost:8080` | Manage models, services, MCP tools |
| **Qdrant** | `http://qdrant:6333` | Vector DB (RAG documents) |

## MCP tools

Check what's active: `data/mcp/servers.txt` or the dashboard MCP tab.

**Typical setup (all via MCP gateway):**
- `n8n` — workflow automation. Set `N8N_API_KEY` in `.env` for full access.
- `playwright` — preferred browser tool (navigate, screenshot, click, fill forms).
- `comfyui` — image/audio/video generation via ComfyUI. For advanced management (queue, workflows, outputs), call the ComfyUI HTTP API directly at `http://comfyui:8188` using fetch.
- `fetch` — parse any URL into readable text.
- `github-official` — needs `GITHUB_PERSONAL_ACCESS_TOKEN` in `.env`

To add a tool: dashboard → MCP tab → add from catalog or paste a Hub URL.

## Models

Primary model: **qwen3.5-uncensored:27b** (`gateway/ollama/qwen3.5-uncensored:27b`)
- 128K context window, balanced speed and reasoning
- Good for: general questions, research, agentic tasks, coding

Other models (switch via config.patch or model selector):
- `gateway/ollama/deepseek-r1:7b` — explicit chain-of-thought reasoning
- `gateway/ollama/deepseek-coder:6.7b` — code-focused fine-tune

## RAG (documents)

Drop files into `data/rag-input/` to auto-embed (requires `--profile rag`).
Or upload directly via Open WebUI → Documents tab.
Qdrant collection: `documents` (check at `http://localhost:6333/dashboard`).

---

*Add your specifics below: SSH hosts, device names, camera streams, etc.*
