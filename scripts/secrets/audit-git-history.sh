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
