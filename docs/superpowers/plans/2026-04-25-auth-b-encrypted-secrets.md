# Auth B — Encrypted Secrets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Encrypt all secrets at rest with SOPS+age in a form safe to commit to a public repo. Move runtime secret files out of paths Hermes' bind-mounts can read. Migrate the highest-value tokens from env vars to Docker secrets (file-form) so they don't appear in `docker inspect`.

**Architecture:** Per-secret encrypted files at `secrets/*.sops` (committable) decrypt to `~/.ai-toolkit/runtime/` (outside any bind-mount) on `make up`. The age private key in `~/.config/sops/age/keys.txt` is the single off-machine secret to safeguard. Internal/low-value tokens (`LITELLM_MASTER_KEY`, `OPS_CONTROLLER_TOKEN`, etc.) stay env-form. High-value tokens (`DISCORD_BOT_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, `HF_TOKEN`, `TAVILY_API_KEY`, `CIVITAI_TOKEN`) move to Docker secrets and services read them via the `_FILE` convention.

**Tech stack:** SOPS, age, GNU Make, Docker Compose secrets, Bash.

**Lifecycle:** This plan lives only on `feat/auth-redesign` and is dropped before the implementation work merges to `main` (see `docs/superpowers/specs/2026-04-25-auth-redesign-design.md` § Lifecycle).

**Independence:** Plan B does not depend on Plans A or C, but **Plan A's `OAUTH2_PROXY_*` tokens** become candidates to migrate into `secrets/.env.sops` once both this plan and Plan A are landed.

---

## File structure

| File | Action | Responsibility |
|------|--------|----------------|
| `secrets/.sops.yaml` | create | SOPS rules: age recipients, file patterns |
| `secrets/.env.sops` | create | SOPS-encrypted env-form tokens (committable) |
| `secrets/discord_token.sops` | create | High-value file-form token (encrypted) |
| `secrets/github_pat.sops` | create | Same |
| `secrets/hf_token.sops` | create | Same |
| `secrets/tavily_key.sops` | create | Same |
| `secrets/civitai_token.sops` | create | Same |
| `secrets/README.md` | create | How to edit + the lifecycle of `~/.ai-toolkit/runtime/` |
| `.gitignore` | modify | Allow `secrets/*.sops`, deny everything else under `secrets/` |
| `Makefile` | modify | Add `up`, `down`, `decrypt-secrets`, `rotate-internal-tokens` targets |
| `scripts/secrets/decrypt.sh` | create | SOPS decrypt loop into `~/.ai-toolkit/runtime/` |
| `scripts/secrets/audit-git-history.sh` | create | Pre-ship grep across `git log -p --all` |
| `docker-compose.yml` | modify | Add `secrets:` block for high-value tokens; `_FILE` env vars on consumers |
| `hermes/entrypoint.sh` | modify | Read `DISCORD_BOT_TOKEN_FILE` if set, else fall back to `DISCORD_BOT_TOKEN` |
| `mcp/gateway/config-loader.sh` | modify | Same pattern for `GITHUB_PERSONAL_ACCESS_TOKEN`, `TAVILY_API_KEY` |
| `tests/test_secrets_isolation.py` | create | Pytest: bind-mount sealing + `docker inspect` plaintext absence |
| `docs/runbooks/secrets.md` | create | Operator runbook |

Branch base: `feat/auth-redesign`.

---

## Task 1: Pre-flight — git-history audit and token rotation

**Files:**
- Create: `scripts/secrets/audit-git-history.sh`

This is the **most important** pre-ship task. The repo is public; if any live token ever landed in a tracked commit, it's already exfiltrated and must be rotated before any of the rest of this plan runs.

- [ ] **Step 1: Write the audit script**

Create `scripts/secrets/audit-git-history.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Audit git history for tokens that may have been accidentally committed.
# Exits non-zero if any pattern matches, so it can gate CI / pre-ship.

cd "$(dirname "$0")/../.."

echo "==> Searching git history for committed .env files..."
if git log --all --diff-filter=A --name-only -- .env | grep -q .env; then
    echo "FAIL: .env was committed in history."
    git log --all --diff-filter=A -- .env | head -20
    exit 1
fi
if git log -p --all -- .env 2>/dev/null | grep -q "^+"; then
    echo "FAIL: .env content appeared in a commit."
    exit 1
fi

echo "==> Searching for known token-format prefixes in tracked history..."
# Public token format prefixes only. These are universal across users
# of each provider; matches indicate accidental commits.
PATTERNS=(
    "github_pat_[A-Za-z0-9_]{30,}"   # GitHub fine-grained PAT
    "ghp_[A-Za-z0-9]{36,}"            # GitHub classic PAT
    "hf_[A-Za-z0-9]{20,}"             # HuggingFace token
    "tvly-[A-Za-z0-9-]{20,}"          # Tavily API key
    "AKIA[0-9A-Z]{16}"                # AWS access key
    "sk-[A-Za-z0-9]{40,}"             # OpenAI/Anthropic key
)

found=0
for pattern in "${PATTERNS[@]}"; do
    if git log -p --all 2>/dev/null | grep -aE "$pattern" | head -1 | grep -q .; then
        echo "FAIL: pattern '$pattern' appears in git history."
        found=1
    fi
done

if [ $found -ne 0 ]; then
    echo ""
    echo "Rotate every matching token before proceeding with the secrets plan."
    exit 1
fi

echo "PASS: no tracked tokens found in history."
```

Make executable: `chmod +x scripts/secrets/audit-git-history.sh`

- [ ] **Step 2: Run it**

`./scripts/secrets/audit-git-history.sh`
Expected: `PASS: no tracked tokens found in history.`

- [ ] **Step 3: If it FAILs**

**STOP** the plan. For each matched token:
- Discord bot: reset at https://discord.com/developers/applications → bot → Reset Token
- GitHub PAT: revoke at https://github.com/settings/tokens
- HuggingFace: regenerate at https://huggingface.co/settings/tokens
- Tavily: regenerate at https://app.tavily.com
- Civitai: regenerate at https://civitai.com/user/account
- Anthropic / OpenAI: revoke at the respective console.

Then update local `.env` with the new tokens. After all tokens are rotated, re-run the audit. Only proceed when it passes.

- [ ] **Step 4: Commit the script**

```bash
git add scripts/secrets/audit-git-history.sh
git commit -m "feat(secrets): add git-history audit script for token leakage"
```

---

## Task 2: Install SOPS + age locally (manual user task)

This is a one-time host setup; no code or commits.

- [ ] **Step 1: Install SOPS**

Pick the right install for your platform; on Windows with `scoop`:
```
scoop install sops age
```
On macOS: `brew install sops age`. On Linux: download from https://github.com/getsops/sops/releases and https://github.com/FiloSottile/age/releases.

Verify: `sops --version` (3.8+); `age --version` (1.1+).

- [ ] **Step 2: Generate an age keypair**

```
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt
chmod 600 ~/.config/sops/age/keys.txt
```

- [ ] **Step 3: Read out and back up the public + private keys**

```
cat ~/.config/sops/age/keys.txt
```

Save the PRIVATE key line (`AGE-SECRET-KEY-1...`) into 1Password / Bitwarden — that's your offline disaster-recovery copy. Save the PUBLIC key line (`# public key: age1...`) — you'll paste it into `secrets/.sops.yaml` next task.

No commit.

---

## Task 3: Configure SOPS to encrypt with your age public key

**Files:**
- Create: `secrets/.sops.yaml`

- [ ] **Step 1: Create `secrets/.sops.yaml` with placeholder recipient**

```yaml
# SOPS configuration. Files matched by `path_regex` are encrypted to the
# `age:` recipients listed here.
#
# Replace YOUR_AGE_PUBLIC_KEY with the `age1...` public key from
# `~/.config/sops/age/keys.txt`. The recipient block IS committed, but
# only holders of the matching private key can decrypt.
creation_rules:
  - path_regex: secrets/.*\.sops$
    age: >-
      YOUR_AGE_PUBLIC_KEY
```

- [ ] **Step 2: Locally fill in the placeholder, do NOT commit your real public key here**

Wait — this is a public-repo decision. The age **public** key is technically safe to publish (it can only encrypt, not decrypt). But publishing it semi-identifies the host. To stay anonymous, replace `YOUR_AGE_PUBLIC_KEY` locally and add `secrets/.sops.yaml` to `.gitignore` (next task) — the file is essential locally but personal.

- [ ] **Step 3: Commit the placeholder version**

(The `.gitignore` rule next task will keep your filled-in copy untracked.)

```bash
git add secrets/.sops.yaml
git commit -m "feat(secrets): scaffold SOPS .sops.yaml with placeholder age recipient"
```

---

## Task 4: Update .gitignore for the secrets/ layout

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Add the secrets/ block**

Append to `.gitignore`:

```gitignore
# SOPS secrets (committable)
!secrets/
secrets/*
!secrets/*.sops
!secrets/.gitkeep
!secrets/README.md
# .sops.yaml is personal (contains your age public key) — gitignored,
# committed in template form via Task 3.
secrets/.sops.yaml
```

- [ ] **Step 2: Verify**

```bash
touch secrets/.gitkeep
git status --short
```
Expected: only `.gitignore` modification staged. The placeholder `.sops.yaml` from Task 3 is now committed AND gitignored — git keeps tracking it but won't surface local edits as untracked. (Use `git update-index --skip-worktree secrets/.sops.yaml` to suppress accidental staging of local edits.)

- [ ] **Step 3: Apply skip-worktree**

```
git update-index --skip-worktree secrets/.sops.yaml
```

- [ ] **Step 4: Commit**

```bash
git add .gitignore secrets/.gitkeep
git commit -m "chore(gitignore): allow secrets/*.sops, hide local .sops.yaml"
```

---

## Task 5: Encrypt the env-form secrets

**Files:**
- Create: `secrets/.env.sops`

- [ ] **Step 1: Stage a plaintext .env.sops locally**

Make a temp file `/tmp/env-secrets-plain` (NOT in the repo) with the env-form secrets that should be encrypted:

```
LITELLM_MASTER_KEY=<your current value from .env>
DASHBOARD_AUTH_TOKEN=<your current value>
OPS_CONTROLLER_TOKEN=<your current value>
THROUGHPUT_RECORD_TOKEN=<your current value>
```

(If Plan A has already shipped, also include `OAUTH2_PROXY_CLIENT_ID`, `OAUTH2_PROXY_CLIENT_SECRET`, `OAUTH2_PROXY_COOKIE_SECRET`.)

- [ ] **Step 2: Encrypt with SOPS**

```
sops --encrypt --input-type=dotenv --output-type=dotenv /tmp/env-secrets-plain > secrets/.env.sops
shred -u /tmp/env-secrets-plain   # or `rm` on Windows
```

- [ ] **Step 3: Verify it round-trips**

```
sops --decrypt secrets/.env.sops | head -2
```
Expected: `LITELLM_MASTER_KEY=...` plaintext.

- [ ] **Step 4: Commit**

```bash
git add secrets/.env.sops
git commit -m "feat(secrets): encrypt env-form internal tokens with SOPS"
```

---

## Task 6: Encrypt each high-value token as a file-form .sops

**Files:**
- Create: `secrets/discord_token.sops`, `secrets/github_pat.sops`, `secrets/hf_token.sops`, `secrets/tavily_key.sops`, `secrets/civitai_token.sops`

Repeat steps for each token. Example for `discord_token`:

- [ ] **Step 1: Encrypt the raw token value**

```
echo -n "$YOUR_DISCORD_BOT_TOKEN" | sops --encrypt --input-type=binary --output-type=binary /dev/stdin > secrets/discord_token.sops
```

- [ ] **Step 2: Verify round-trip**

```
sops --decrypt secrets/discord_token.sops | wc -c
```
Expected: byte count matches the original token length.

- [ ] **Step 3: Repeat for each remaining high-value token**

```
echo -n "$GITHUB_PAT" | sops --encrypt --input-type=binary --output-type=binary /dev/stdin > secrets/github_pat.sops
echo -n "$HF_TOKEN" | sops --encrypt --input-type=binary --output-type=binary /dev/stdin > secrets/hf_token.sops
echo -n "$TAVILY_KEY" | sops --encrypt --input-type=binary --output-type=binary /dev/stdin > secrets/tavily_key.sops
echo -n "$CIVITAI_TOKEN" | sops --encrypt --input-type=binary --output-type=binary /dev/stdin > secrets/civitai_token.sops
```

- [ ] **Step 4: Commit all five**

```bash
git add secrets/*.sops
git commit -m "feat(secrets): encrypt high-value tokens (discord/github/hf/tavily/civitai) for Docker secrets"
```

---

## Task 7: Write the decrypt loop script

**Files:**
- Create: `scripts/secrets/decrypt.sh`

- [ ] **Step 1: Write the script**

Create `scripts/secrets/decrypt.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Decrypt SOPS-encrypted secrets into ~/.ai-toolkit/runtime/.
# Runs at `make up`. Requires age key at ~/.config/sops/age/keys.txt.

cd "$(dirname "$0")/../.."

if [ ! -f "${HOME}/.config/sops/age/keys.txt" ]; then
    echo "ERROR: age key not found at ~/.config/sops/age/keys.txt." >&2
    echo "Generate one with: age-keygen -o ~/.config/sops/age/keys.txt" >&2
    exit 1
fi

RUNTIME_DIR="${HOME}/.ai-toolkit/runtime"
SECRETS_DIR="${RUNTIME_DIR}/secrets"
mkdir -p "$SECRETS_DIR"
chmod 700 "$RUNTIME_DIR" "$SECRETS_DIR"

# Env-form: decrypt to a single .env file (mode 600).
sops --decrypt secrets/.env.sops > "${RUNTIME_DIR}/.env"
chmod 600 "${RUNTIME_DIR}/.env"
echo "==> ${RUNTIME_DIR}/.env (env-form internal tokens)"

# File-form: decrypt each high-value token to its own file (mode 600).
for src in secrets/discord_token.sops secrets/github_pat.sops \
           secrets/hf_token.sops secrets/tavily_key.sops \
           secrets/civitai_token.sops; do
    name=$(basename "$src" .sops)
    sops --decrypt "$src" > "${SECRETS_DIR}/${name}"
    chmod 600 "${SECRETS_DIR}/${name}"
    echo "==> ${SECRETS_DIR}/${name}"
done

echo ""
echo "Runtime secrets written under ${RUNTIME_DIR}."
```

`chmod +x scripts/secrets/decrypt.sh`

- [ ] **Step 2: Test it**

```
./scripts/secrets/decrypt.sh
ls -la ~/.ai-toolkit/runtime/
ls -la ~/.ai-toolkit/runtime/secrets/
```
Expected: `.env` plus 5 token files, all mode `-rw-------`.

- [ ] **Step 3: Verify the runtime dir is OUTSIDE `/c/dev` and `/workspace`**

```
realpath ~/.ai-toolkit/runtime/.env
```
Expected: a path under your home (e.g. `/c/Users/lynch/.ai-toolkit/runtime/.env`), **not** under `C:\dev\AI-toolkit` or `/workspace`. That's the bind-mount-sealing property.

- [ ] **Step 4: Commit**

```bash
git add scripts/secrets/decrypt.sh
git commit -m "feat(secrets): SOPS decrypt script writes runtime/ outside bind-mounts"
```

---

## Task 8: Wire decrypt into the Makefile

**Files:**
- Modify: `Makefile`

- [ ] **Step 1: Add the decrypt target**

If `Makefile` has no `up` target yet (the user invokes `docker compose up -d` directly), introduce one. If it does, prepend the decrypt step.

```makefile
RUNTIME_ENV := $(HOME)/.ai-toolkit/runtime/.env

.PHONY: decrypt-secrets up down logs

decrypt-secrets:
	@./scripts/secrets/decrypt.sh

up: decrypt-secrets
	docker compose --env-file $(RUNTIME_ENV) up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=100
```

- [ ] **Step 2: Verify**

```
make up
```
Expected: decrypts secrets, then `docker compose up -d` runs with the runtime env file.

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "feat(make): add up/down/decrypt-secrets targets pointing at runtime env"
```

---

## Task 9: Migrate high-value tokens to Docker secrets in compose

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add the top-level `secrets:` block**

At the bottom of `docker-compose.yml` (alongside `volumes:` and `networks:`):

```yaml
secrets:
  discord_token:
    file: ${HOME}/.ai-toolkit/runtime/secrets/discord_token
  github_pat:
    file: ${HOME}/.ai-toolkit/runtime/secrets/github_pat
  hf_token:
    file: ${HOME}/.ai-toolkit/runtime/secrets/hf_token
  tavily_key:
    file: ${HOME}/.ai-toolkit/runtime/secrets/tavily_key
  civitai_token:
    file: ${HOME}/.ai-toolkit/runtime/secrets/civitai_token
```

- [ ] **Step 2: Attach `discord_token` to hermes-gateway only**

In the `hermes-gateway` service block, REMOVE:

```yaml
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN:-}
```

ADD:

```yaml
      - DISCORD_BOT_TOKEN_FILE=/run/secrets/discord_token
    secrets:
      - discord_token
```

- [ ] **Step 3: Attach `github_pat` and `tavily_key` to mcp-gateway**

In `mcp-gateway`'s environment block, REMOVE:

```yaml
      - GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_PERSONAL_ACCESS_TOKEN:-}
      - TAVILY_API_KEY=${TAVILY_API_KEY:-}
```

ADD:

```yaml
      - GITHUB_PERSONAL_ACCESS_TOKEN_FILE=/run/secrets/github_pat
      - TAVILY_API_KEY_FILE=/run/secrets/tavily_key
    secrets:
      - github_pat
      - tavily_key
```

- [ ] **Step 4: Attach `hf_token` to gguf-puller and ops-controller**

For each service that consumes `HF_TOKEN`, replace the env-var with `_FILE` and attach the secret:

```yaml
      - HF_TOKEN_FILE=/run/secrets/hf_token
    secrets:
      - hf_token
```

- [ ] **Step 5: Attach `civitai_token` to comfyui-model-puller**

Same pattern.

- [ ] **Step 6: Verify config parses**

`docker compose config | grep -E "(secrets:|TOKEN_FILE|/run/secrets)"`
Expected: secrets block present; consumer services reference `_FILE` paths.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(secrets): migrate high-value tokens to Docker secrets (file-form)"
```

---

## Task 10: Update entrypoints to read `_FILE` env vars

**Files:**
- Modify: `hermes/entrypoint.sh`
- Modify: `mcp/gateway/config-loader.sh` (or equivalent — check the actual mcp-gateway image's entry path)

For each entrypoint that consumes a token, add a "read from file if `_FILE` is set" pattern.

- [ ] **Step 1: Patch hermes/entrypoint.sh**

Add early in the script (before `exec hermes ...`):

```bash
# Read token from _FILE if set (Docker secrets pattern), else fall back
# to the env var (kept for backward compat).
if [ -n "${DISCORD_BOT_TOKEN_FILE:-}" ] && [ -f "$DISCORD_BOT_TOKEN_FILE" ]; then
    export DISCORD_BOT_TOKEN="$(cat "$DISCORD_BOT_TOKEN_FILE")"
fi
```

- [ ] **Step 2: Patch mcp-gateway loader**

Same pattern, applied to `GITHUB_PERSONAL_ACCESS_TOKEN`, `TAVILY_API_KEY`, `HF_TOKEN`, `CIVITAI_TOKEN` as appropriate. Confirm the actual entry path in `mcp/gateway/` (could be a Python loader or a shell wrapper).

- [ ] **Step 3: Verify**

`docker compose up -d --force-recreate hermes-gateway mcp-gateway`
`docker logs ordo-ai-stack-hermes-gateway-1 2>&1 | grep -i discord`
Expected: Discord bot connects (no token-missing error).

- [ ] **Step 4: Commit**

```bash
git add hermes/entrypoint.sh mcp/gateway/config-loader.sh
git commit -m "feat(secrets): read tokens from _FILE env vars (Docker secrets pattern)"
```

---

## Task 11: Remove migrated tokens from .env.example

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Replace each migrated token's section with a SOPS pointer**

For DISCORD_BOT_TOKEN, GITHUB_PERSONAL_ACCESS_TOKEN, HF_TOKEN, TAVILY_API_KEY, CIVITAI_TOKEN — replace the existing inline `# TOKEN=` examples with:

```bash
# DISCORD_BOT_TOKEN: managed via SOPS at secrets/discord_token.sops.
# Edit with: sops secrets/discord_token.sops
# Decrypted runtime path: ~/.ai-toolkit/runtime/secrets/discord_token.
# See docs/runbooks/secrets.md.
```

(Same comment template for each, with the right file name.)

- [ ] **Step 2: Verify env vars no longer appear as examples**

`grep -E "^# (DISCORD_BOT_TOKEN|GITHUB_PERSONAL_ACCESS_TOKEN|HF_TOKEN|TAVILY_API_KEY|CIVITAI_TOKEN)=" .env.example`
Expected: no matches (all replaced with SOPS pointer comments).

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(secrets): point .env.example at SOPS for migrated high-value tokens"
```

---

## Task 12: Test bind-mount sealing (TDD)

**Files:**
- Create: `tests/test_secrets_isolation.py`

- [ ] **Step 1: Write the test**

Create `tests/test_secrets_isolation.py`:

```python
"""Verify Hermes' bind-mounts cannot see decrypted runtime secrets."""
import json
import os
import subprocess


def _docker_exec(container: str, *cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container, *cmd],
        capture_output=True,
        text=True,
    )


def test_runtime_env_not_visible_in_workspace():
    """From inside hermes-gateway, /workspace/.env should NOT exist."""
    r = _docker_exec("ordo-ai-stack-hermes-gateway-1", "test", "-f", "/workspace/.env")
    assert r.returncode != 0, "FAIL: /workspace/.env exists inside Hermes — secret leakage path open"


def test_runtime_secrets_dir_not_visible_in_workspace():
    """No path under /workspace should hold the decrypted runtime secrets."""
    r = _docker_exec(
        "ordo-ai-stack-hermes-gateway-1",
        "find", "/workspace", "-maxdepth", "3", "-name", "discord_token",
    )
    assert r.stdout.strip() == "", f"FAIL: discord_token visible at {r.stdout!r}"


def test_high_value_token_not_in_docker_inspect():
    """`docker inspect hermes-gateway` should not contain the plaintext Discord token."""
    inspect = subprocess.run(
        ["docker", "inspect", "ordo-ai-stack-hermes-gateway-1"],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(inspect.stdout)
    env = parsed[0]["Config"]["Env"]
    # DISCORD_BOT_TOKEN as a plaintext env var should be absent.
    plaintext = [e for e in env if e.startswith("DISCORD_BOT_TOKEN=")]
    assert plaintext == [], f"FAIL: plaintext DISCORD_BOT_TOKEN env var present: {plaintext}"
    # The _FILE pointer is fine (and expected).
    pointer = [e for e in env if e.startswith("DISCORD_BOT_TOKEN_FILE=")]
    assert pointer, "FAIL: DISCORD_BOT_TOKEN_FILE pointer missing — wiring incomplete"


def test_secret_file_inside_container_is_readable():
    """The Docker secret file should be readable by the service inside its container."""
    r = _docker_exec(
        "ordo-ai-stack-hermes-gateway-1",
        "test", "-r", "/run/secrets/discord_token",
    )
    assert r.returncode == 0, "FAIL: /run/secrets/discord_token not readable inside container"
```

- [ ] **Step 2: Run, verify pass (assuming Tasks 9-10 are complete)**

`pytest tests/test_secrets_isolation.py -v`
Expected: 4/4 pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_secrets_isolation.py
git commit -m "test(secrets): bind-mount sealing + docker-inspect plaintext absence"
```

---

## Task 13: Token-rotation script for internal tokens

**Files:**
- Modify: `Makefile`
- Create: `scripts/secrets/rotate-internal.sh`

- [ ] **Step 1: Write the rotation script**

Create `scripts/secrets/rotate-internal.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

if [ ! -f "${HOME}/.config/sops/age/keys.txt" ]; then
    echo "ERROR: age key required for rotation." >&2
    exit 1
fi

# Generate new internal tokens.
NEW_LITELLM=$(openssl rand -hex 32)
NEW_DASHBOARD=$(openssl rand -hex 32)
NEW_OPS=$(openssl rand -hex 32)
NEW_THROUGHPUT=$(openssl rand -hex 32)

# Decrypt current, replace the listed tokens, re-encrypt.
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
sops --decrypt secrets/.env.sops > "$TMP"

# Replace each token line in-place (works on values that may contain '=').
python3 - "$TMP" <<PY
import sys, re
path = sys.argv[1]
text = open(path).read()
replacements = {
    "LITELLM_MASTER_KEY": "$NEW_LITELLM",
    "DASHBOARD_AUTH_TOKEN": "$NEW_DASHBOARD",
    "OPS_CONTROLLER_TOKEN": "$NEW_OPS",
    "THROUGHPUT_RECORD_TOKEN": "$NEW_THROUGHPUT",
}
for k, v in replacements.items():
    text = re.sub(rf"^{k}=.*$", f"{k}={v}", text, flags=re.MULTILINE)
open(path, "w").write(text)
PY

sops --encrypt --input-type=dotenv --output-type=dotenv "$TMP" > secrets/.env.sops

echo "==> Internal tokens rotated. Now restart dependent services:"
echo "    docker compose restart model-gateway dashboard ops-controller"
```

`chmod +x scripts/secrets/rotate-internal.sh`

- [ ] **Step 2: Add Makefile target**

Append to `Makefile`:

```makefile
.PHONY: rotate-internal-tokens

rotate-internal-tokens:
	@./scripts/secrets/rotate-internal.sh
```

- [ ] **Step 3: Test it (in a worktree to avoid clobbering live secrets)**

```
make rotate-internal-tokens
make decrypt-secrets
diff <(git show HEAD:secrets/.env.sops) secrets/.env.sops || echo "(differs — rotation worked)"
```

- [ ] **Step 4: Commit**

```bash
git add scripts/secrets/rotate-internal.sh Makefile
git commit -m "feat(secrets): make rotate-internal-tokens regenerates internal bearer tokens"
```

---

## Task 14: Operator runbook

**Files:**
- Create: `docs/runbooks/secrets.md`
- Create: `secrets/README.md`

- [ ] **Step 1: Write the operator runbook**

Create `docs/runbooks/secrets.md`:

```markdown
# Secrets — Operator Runbook

## Mental model

- **One thing to safeguard**: `~/.config/sops/age/keys.txt` (your age private key).
- All other secrets are encrypted-at-rest in `secrets/*.sops` (committed to
  the public repo) and decrypted into `~/.ai-toolkit/runtime/` only when
  needed.
- The runtime directory is **outside** `/workspace` and the `HERMES_HOST_DEV_MOUNT`
  bind-mount, so even a prompt-injected Hermes can't `cat` the decrypted
  files.
- High-value tokens (Discord, GitHub PAT, HF, Tavily, Civitai) are mounted
  into containers as **Docker secrets** (files at `/run/secrets/<name>`),
  not env vars — so they don't appear in `docker inspect`.

## First-time setup

1. Install: `scoop install sops age` (or `brew install sops age`).
2. Generate keypair: `age-keygen -o ~/.config/sops/age/keys.txt && chmod 600 $_`.
3. Back up the private key line (`AGE-SECRET-KEY-1...`) to 1Password.
4. Paste the public key (`# public key: age1...`) into `secrets/.sops.yaml`
   under the `age:` recipient list (locally only; file is gitignored).
5. `git update-index --skip-worktree secrets/.sops.yaml` to suppress
   accidental staging.
6. `make up` — decrypts secrets and brings up the stack.

## Edit a secret

```
sops secrets/.env.sops              # opens decrypted in $EDITOR, re-encrypts on save
sops secrets/discord_token.sops     # same for individual file-form tokens
```

After editing, restart the dependent service:
```
docker compose restart hermes-gateway   # for Discord
docker compose restart mcp-gateway      # for GitHub PAT, Tavily
```

## Rotate internal tokens

```
make rotate-internal-tokens
docker compose restart model-gateway dashboard ops-controller
```

## Rotate high-value tokens (issuer-side)

For each, regenerate at the provider, then:
```
echo -n "$NEW_VALUE" | sops --encrypt --input-type=binary --output-type=binary /dev/stdin > secrets/<name>.sops
make decrypt-secrets
docker compose restart <service-that-uses-it>
```

## Recovery — age key lost

Restore from 1Password backup. Without it, **none** of `secrets/*.sops`
can be decrypted. The repo is recoverable (re-generate every secret), but
the recovery is painful — back up the key.

## Recovery — age key leaked

Treat as catastrophic:
1. Generate new keypair: `age-keygen -o ~/.config/sops/age/keys.txt.new`.
2. Update `secrets/.sops.yaml` with the new public key.
3. For each `secrets/*.sops`: decrypt with old key, re-encrypt with new key.
4. Force-push `secrets/` (rewrites history of encrypted blobs — encrypted
   contents change but plaintext stays).
5. Rotate every actual token at its provider (the old encrypted blobs are
   forever-decryptable by anyone with the leaked key, even after force-push,
   because they may have been mirrored).
6. Run `scripts/secrets/audit-git-history.sh` to confirm clean state.

## Audit history

`./scripts/secrets/audit-git-history.sh` — runs the public-prefix grep over
`git log -p --all`. Hook into pre-commit if you want.
```

- [ ] **Step 2: Write secrets/README.md**

```markdown
# secrets/

Encrypted-at-rest secrets for the Ordo AI stack. **All `*.sops` files in
this directory are safe to commit to a public repo** — they decrypt only
with the age private key at `~/.config/sops/age/keys.txt`.

- `.env.sops` — env-form internal tokens (LITELLM_MASTER_KEY, etc.)
- `discord_token.sops`, `github_pat.sops`, `hf_token.sops`,
  `tavily_key.sops`, `civitai_token.sops` — high-value file-form tokens.

To edit: `sops secrets/<file>.sops`.
To decrypt: `make decrypt-secrets` writes plaintext to `~/.ai-toolkit/runtime/`.

See `docs/runbooks/secrets.md` for the full lifecycle.
```

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/secrets.md secrets/README.md
git commit -m "docs(secrets): operator runbook + secrets/ README"
```

---

## Self-review checklist

- [ ] Every task has concrete file paths and complete code/scripts (no "TBD" or "see above").
- [ ] Spec coverage: § Secrets handling > File layout (Tasks 3-7), Form-by-form classification (Tasks 5, 6, 9, 10), Why two forms (Task 12 tests), Key management (Tasks 2, 14), § Pre-ship checklist git-history audit (Task 1), § Failure modes age key lost / leaked / token leaks (Tasks 13, 14), § Testing — Cold start with secrets, Bind-mount sealing (Task 12).
- [ ] Spec items deferred to Plan A: SSO env vars (`OAUTH2_PROXY_*`) noted as "if Plan A has shipped, also include" in Task 5.
- [ ] Spec items deferred to Plan C: Hermes Docker socket removal (Plan C), ops-controller verbs (Plan C).
- [ ] Type/method consistency: `~/.ai-toolkit/runtime/` path used identically in spec, Task 7 script, Task 8 Makefile, Task 12 tests, Task 14 runbook.
- [ ] Public-repo safety: no real tokens, real emails, or real age public keys committed in this plan's tasks. `secrets/.sops.yaml` is templated with placeholder + gitignored locally.
