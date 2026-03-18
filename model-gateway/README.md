# Model Gateway

OpenAI-compatible proxy for unified model access. Routes chat and embedding requests to Ollama (and future providers like vLLM). Also serves as an Anthropic-compatible proxy for Claude Code local model support.

**Status:** See [Product Requirements Document](../docs/Product%20Requirements%20Document.md) for design and decisions.

## Endpoints

- `GET /v1/models` — List models from all providers
- `POST /v1/chat/completions` — Chat completion (streaming supported)
- `POST /v1/messages` — Anthropic Messages API (Claude Code compatibility)
- `POST /v1/embeddings` — Embeddings
- `GET /health` — Gateway health

## Config

| Variable | Description |
|----------|-------------|
| `OLLAMA_URL` | Upstream Ollama URL (default: `http://ollama:11434`) |
| `VLLM_URL` | Optional vLLM backend URL |
| `DEFAULT_PROVIDER` | Default provider when no prefix (default: `ollama`) |
| `CLAUDE_CODE_LOCAL_MODEL` | Default local model for Claude Code `claude-*` remapping (e.g. `glm-4.7-flash:Q4_K_M`) |
| `OLLAMA_NUM_CTX` | KV cache context cap (default: `16384`, `0` = model max) |
| `MODEL_CACHE_TTL_SEC` | Model list cache TTL (default: `60`) |

## Claude Code with local models

Run Claude Code against any Ollama model via the gateway's Anthropic Messages API translation.

### Setup

Add to your shell profile (`~/.bashrc`, `~/.zshrc`, or PowerShell `$PROFILE`):

```bash
# Bash / Zsh — add to ~/.bashrc or ~/.zshrc
export ANTHROPIC_AUTH_TOKEN="ollama"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_BASE_URL="http://localhost:11435"
```

```powershell
# PowerShell — add to $PROFILE
$env:ANTHROPIC_AUTH_TOKEN = "ollama"
$env:ANTHROPIC_API_KEY = ""
$env:ANTHROPIC_BASE_URL = "http://localhost:11435"
```

> **Important:** `ANTHROPIC_API_KEY` must be an empty string (`""`), not unset. This tells Claude Code to use the auth token path, which allows custom model names.

Then reload your shell:

```bash
source ~/.bashrc   # or source ~/.zshrc
```

### Usage — specify the model directly

```bash
claude --model devstral-small-2
claude --model glm-4.7-flash:Q4_K_M
claude --model qwen3.5-uncensored:27b
```

Any Ollama model name works — the gateway passes it through to Ollama as-is.

### Usage — use the default model

If `CLAUDE_CODE_LOCAL_MODEL` is set in `.env`, you can just run `claude` without `--model`. Claude Code sends a `claude-*` model name by default, and the gateway remaps it to the configured local model.

```bash
# .env
CLAUDE_CODE_LOCAL_MODEL=glm-4.7-flash:Q4_K_M
```

```bash
# Then just:
claude
```

### Remote machine

Point Claude Code at the machine running the gateway:

```bash
export ANTHROPIC_AUTH_TOKEN="ollama"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_BASE_URL="http://<gateway-host-ip>:11435"
claude --model glm-4.7-flash:Q4_K_M
```

Replace `<gateway-host-ip>` with the IP or hostname of the machine running the AI toolkit. Port `11435` must be reachable (check firewall).

**Verify connectivity first:**
```bash
curl http://<gateway-host-ip>:11435/health
```

### How it works

1. Claude Code sends requests to `/v1/messages` (Anthropic Messages API)
2. If the model name is `claude-*`, the gateway remaps it to `CLAUDE_CODE_LOCAL_MODEL`
3. Otherwise, the model name passes through as-is (e.g. `devstral-small-2`)
4. The request is translated from Anthropic format to Ollama's `/api/chat`
5. The response is translated back to Anthropic format

Claude Code doesn't know it's talking to a local model — the gateway is a transparent proxy.

### Changing the default model

Edit `CLAUDE_CODE_LOCAL_MODEL` in `.env` and restart:

```bash
docker compose up -d model-gateway
```
