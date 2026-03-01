# TOOLS.md

Environment-specific notes. Skills define how tools work; this file is for your setup.

## AI-toolkit Stack

| Service        | Purpose                                      |
|----------------|----------------------------------------------|
| **Ollama**     | Local LLMs at `http://ollama:11434`          |
| **Model Gateway** | OpenAI-compatible at `http://model-gateway:11435/v1` |
| **MCP Gateway**   | Tools at `http://mcp-gateway:8811/mcp`       |

Trust boundary: credentials stay in the controller. Browser worker (if used) is untrusted.

---

Add your specifics: camera names, SSH hosts, TTS voices, device nicknames, etc.
