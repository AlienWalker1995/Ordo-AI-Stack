# Secrets — Operator Runbook

## Mental model

- **One thing to safeguard**: `~/.config/sops/age/keys.txt` (your age private key).
- All other secrets are encrypted at rest in `secrets/*.sops` (committed to
  the public repo) and decrypted into `~/.ai-toolkit/runtime/` only when
  needed.
- The runtime directory is **outside** `/workspace` and the
  `HERMES_HOST_DEV_MOUNT` bind-mount, so even a prompt-injected Hermes
  cannot `cat` the decrypted files.
- High-value tokens (Discord, GitHub PAT, HF, Tavily, Civitai) are mounted
  into containers as **Docker secrets** (files at `/run/secrets/<name>`),
  not env vars — so they don't appear in `docker inspect`.

## First-time setup

1. Install: `winget install Mozilla.sops FiloSottile.age` (Windows) or
   `brew install sops age` (macOS) or download from each project's
   GitHub releases (Linux).
2. Generate a keypair:
   ```
   mkdir -p ~/.config/sops/age
   age-keygen -o ~/.config/sops/age/keys.txt
   chmod 600 ~/.config/sops/age/keys.txt
   ```
3. Back up the private key line (`AGE-SECRET-KEY-1...`) to a password
   manager (1Password / Bitwarden / LastPass) under an entry titled
   "Ordo SOPS age key — disaster recovery."
4. Copy the public key line (`age1...`) and paste it into
   `secrets/.sops.yaml` under the `creation_rules.[*].age` field.
   The public key is safe to commit; only the matching private key
   can decrypt.
5. `make up` — decrypts secrets and brings up the stack.

## Edit a secret

```
sops secrets/.env.sops              # opens decrypted in $EDITOR, re-encrypts on save
sops secrets/discord_token.sops     # same for individual file-form tokens
```

If your editor isn't picking up dotenv format on the env file, set
`SOPS_EDITOR_VERSION=2` in your shell or pass `--input-type=dotenv`
explicitly.

After editing, restart the dependent service:
```
docker compose restart hermes-gateway   # for Discord
docker compose restart mcp-gateway      # for GitHub PAT, Tavily
docker compose restart ops-controller   # for HF
```

## Rotate internal tokens

Internal tokens (`LITELLM_MASTER_KEY`, `DASHBOARD_AUTH_TOKEN`,
`OPS_CONTROLLER_TOKEN`, `OAUTH2_PROXY_COOKIE_SECRET`) live inside
`secrets/.env.sops`. Rotate all of them at once:

```
make rotate-internal-tokens
make decrypt-secrets
docker compose restart model-gateway dashboard ops-controller \
    worker hermes-gateway hermes-dashboard mcp-gateway oauth2-proxy
git add secrets/.env.sops
git commit -m "chore(secrets): rotate internal tokens"
git push
```

The cookie-secret rotation invalidates every existing oauth2-proxy
session — you'll re-sign-in via Google after the restart.

## Rotate high-value tokens (issuer-side)

Each provider's web UI is the source of truth — regenerate there first,
then re-encrypt the new value:

| Provider | Where to regenerate |
|---|---|
| Discord bot | https://discord.com/developers/applications → bot → Reset Token |
| GitHub PAT | https://github.com/settings/tokens (revoke + create) |
| HuggingFace | https://huggingface.co/settings/tokens |
| Tavily | https://app.tavily.com → Settings → API Keys |
| Civitai | https://civitai.com/user/account → API Keys |

Then on the host:
```
echo -n "$NEW_VALUE" | \
  sops --encrypt --age "$(grep '^# public key:' ~/.config/sops/age/keys.txt | awk '{print $4}')" \
       --input-type=binary --output-type=binary /dev/stdin \
       > secrets/<name>.sops
make decrypt-secrets
docker compose restart <consumer-service>
git add secrets/<name>.sops && git commit && git push
```

## Recovery — age key lost

Restore the private key from your password-manager backup. Without it,
none of `secrets/*.sops` can be decrypted. The repo is recoverable
(re-generate every secret at the provider, re-encrypt with a new key)
but the recovery is painful — back up the key.

## Recovery — age key leaked

Treat as catastrophic:

1. Generate a new keypair: `age-keygen -o ~/.config/sops/age/keys.txt.new`.
2. Update `secrets/.sops.yaml` with the new public key.
3. For each `secrets/*.sops`: decrypt with the old key, re-encrypt with
   the new key.
4. Force-push `secrets/` (the encrypted blobs change, but plaintext stays).
5. Rotate every actual token at its provider — the old encrypted blobs
   are forever-decryptable by anyone with the leaked key, even after
   force-push, because they may have been mirrored.
6. Run `scripts/secrets/audit-git-history.sh` to confirm a clean state.

## Audit history

```
./scripts/secrets/audit-git-history.sh
```

Searches `git log -p --all` for known token-format prefixes (GitHub PAT,
HuggingFace, Tavily, etc.). Hook into pre-commit if you want.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `make decrypt-secrets` fails with `Failed to get the data key` | `SOPS_AGE_KEY_FILE` env var not set or key file unreadable | Set `SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/keys.txt` and verify `chmod 600` |
| `Error unmarshalling input json: invalid character` on .env.sops decrypt | SOPS 3.7.x doesn't auto-detect dotenv format | Use `--input-type=dotenv --output-type=dotenv` flags. The decrypt script already does this. |
| Container starts but immediately exits with `cookie_secret must be 16, 24, or 32 bytes` | `OAUTH2_PROXY_COOKIE_SECRET` was generated with `openssl rand -base64 32` (44 chars) | Regenerate with `LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom \| head -c 32`, edit `secrets/.env.sops`, restart |
| Hermes can't reach Discord but token "looks right" | Bridge from `_FILE` to env var didn't run | Confirm `hermes/entrypoint.sh` sources the bridge BEFORE calling the Discord SDK, and that the secret file at `/run/secrets/discord_token` exists in the container |
| `docker compose up` fails with `secret "discord_token" file is not specified` | The compose `secrets:` block at file top points to a path that doesn't exist | Run `make decrypt-secrets` first to populate `~/.ai-toolkit/runtime/secrets/` |
