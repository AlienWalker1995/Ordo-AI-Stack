# Rotation Checklist

**Use this checklist if secrets were ever committed to the repository or exposed.**

---

## When to use

- You accidentally committed `.env` or `openclaw/.env` and pushed to a remote
- A secret was found in git history
- You suspect a token or key was leaked

---

## Current status (as of assessment)

- **Git history:** No secrets found in committed files
- **Working tree:** `.env` and `openclaw/.env` are gitignored; if they exist locally with real values, they have not been committed

**No rotation required** based on the assessment. Keep this checklist for future reference.

---

## If rotation is needed

### 1. OpenClaw gateway token

- **Location:** `OPENCLAW_GATEWAY_TOKEN` in `.env` and `openclaw/.env`
- **Actions:**
  1. Generate new token: `openssl rand -hex 32`
  2. Update `.env` and `openclaw/.env`
  3. Restart OpenClaw gateway
  4. Re-pair devices if using remote access

### 2. Telegram bot token

- **Location:** `TELEGRAM_BOT_TOKEN` in `openclaw/.env` (if used)
- **Actions:**
  1. Revoke at [@BotFather](https://t.me/BotFather) → /revoke
  2. Create new token
  3. Update `openclaw/.env`
  4. Restart OpenClaw

### 3. Model API keys (OpenAI, Anthropic, Gemini, OpenRouter)

- **Location:** `openclaw/.env`
- **Actions:**
  1. Rotate in respective provider dashboards
  2. Update `openclaw/.env`
  3. Restart OpenClaw

### 4. Claude AI / Web session keys (if used)

- **Location:** `CLAUDE_AI_SESSION_KEY`, `CLAUDE_WEB_SESSION_KEY`, `CLAUDE_WEB_COOKIE` in `openclaw/.env`
- **Actions:**
  1. Revoke sessions in Claude settings
  2. Generate new session keys
  3. Update `openclaw/.env`

---

## After rotation

1. Ensure `.env` and `openclaw/.env` are in `.gitignore` and never committed
2. If secrets were in git history, consider `git filter-repo` or BFG to purge (requires force push; coordinate with collaborators)
3. Add pre-commit hook and CI secret scanning to prevent recurrence
