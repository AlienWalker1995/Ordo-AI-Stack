# Secrets — Operator Runbook

## Mental model

- **One thing to safeguard**: `~/.config/sops/age/keys.txt` (your age private key).
- All other secrets are encrypted at rest in `secrets/*.sops` (committed to
  the public repo). File-form tokens decrypt into
  `~/.ai-toolkit/runtime/secrets/`; env-form internal tokens are materialized
  into the gitignored `out/secrets.env` (which the rendered compose reads
  directly) — both only when needed.
- `~/.ai-toolkit/runtime/secrets/` is **outside** `/workspace` and the
  Hermes `/c/dev` mirror-mount, so a prompt-injected Hermes cannot `cat`
  those files. `out/secrets.env` is **not** — it lives under the
  `/c/dev` tree the `agent` container mounts, so treat it with the same
  care as any other file in the repo working copy.
- High-value tokens (Discord, GitHub backup PAT) are mounted into
  containers as **Docker secrets** (files at `/run/secrets/<name>`), not
  env vars — so they don't appear in `docker inspect`. Web search is the
  self-hosted SearXNG MCP, which needs no external API key.

## How services receive secrets at runtime

Two delivery paths:

- **Env-form** (`secrets/.env.sops` → `out/secrets.env`): `ordo render`
  emits `out/secrets.env.example` listing the required KEYS (values
  empty). Copy it to the gitignored `out/secrets.env` and fill in the
  real values — by hand, or via `sops --decrypt --input-type=dotenv
  --output-type=dotenv secrets/.env.sops`. The rendered
  `out/docker-compose.yml` already declares `secrets.env` as a second,
  `required: false` `env_file` alongside the derived `out/.env` on every
  service that needs it, so a plain `docker compose -p ordo up -d`, run from
  `out/`, picks up the real values (`OAUTH2_PROXY_*`, `OPS_CONTROLLER_TOKEN`,
  `LITELLM_MASTER_KEY`, `SEARXNG_SECRET`, `N8N_API_KEY`, …) with no
  `--env-file` flags needed.
- **File-form** (`secrets/<name>.sops` → `runtime/secrets/<name>`): mounted as
  Docker secrets at `/run/secrets/<name>`, so they never appear in
  `docker inspect`. Where an app SDK expects a plain env var, the consumer's
  entrypoint **bridges** `<NAME>_FILE` → a `<NAME>` env var (e.g. the `agent`
  service: `DISCORD_BOT_TOKEN`, `GITHUB_BACKUP_PAT`). So agents read the token from their
  environment — they never see, need, or look for a plaintext secret in `.env`.
  The `ordo-hermes-backup` git remote authenticates the same way: a credential
  helper reads the SOPS-decrypted token, so no token is ever embedded in a URL.

### ops-api recreates with real secrets

The dashboard backend (`ops-api`) recreates services in-container via its own
`docker-compose` subprocess (the dashboard "recreate" and `POST /compose/*`
paths). Because the `ops-api` container itself loads `out/secrets.env` as
an `env_file`, its process already has the real values; it passes that
environment through to the subprocess (`_compose_env` in
`services/ops-api/main.py`), so a secret-dependent service it recreates
(oauth2-proxy, caddy, searxng, n8n, dashboard, model-gateway/litellm) gets its
real values — instead of coming up unset and crash-looping (e.g. oauth2-proxy
on an 11-byte `placeholder` cookie secret). `ops-api` never holds the age key:
decryption of the `.sops` blobs stays a host-only operation. (The GPU
scheduler keeps the separate `ops-controller` service name — its live clients
depend on it — but the secret-aware recreate path lives in `ops-api`.)

> **Never paste secrets or the age key into chat, a log, or an issue, and never
> "fix" a secret-stripped service by writing placeholder values into `.env` or
> stubbing empty `secrets/*` files.** A `missing setting` / `not a directory`
> error from a secret service means `out/secrets.env` (or, for file-form
> tokens, `~/.ai-toolkit/runtime/secrets/`) wasn't populated — the fix is
> filling it on the host, not a synthesized value.

**Boundary:** secrets stay local. Only the encrypted `.sops` blobs plus
architecture/config (compose, this runbook) are published; plaintext lands in
`~/.ai-toolkit/runtime/secrets/` (file-form, outside `/workspace` and the
Hermes `/c/dev` mount) and in the gitignored `out/secrets.env` (env-form,
which **is** inside the Hermes `/c/dev` mirror-mount).

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
5. Render the stack (`python -m ordo.cli render --out out`, from the repo root),
   copy `out/secrets.env.example` to `out/secrets.env` and fill in real
   values, then bring it up: `docker compose -p ordo up -d`, run from
   `out/` (see `docs/operator-guide.md`).

## Edit a secret

```
sops secrets/.env.sops              # opens decrypted in $EDITOR, re-encrypts on save
sops secrets/discord_token.sops     # same for individual file-form tokens
```

If your editor isn't picking up dotenv format on the env file, set
`SOPS_EDITOR_VERSION=2` in your shell or pass `--input-type=dotenv`
explicitly.

For env-form keys, also copy the changed value into the rendered
`out/secrets.env` — that's the file compose actually reads; re-encrypting
`secrets/.env.sops` alone doesn't propagate to it.

After editing, restart the dependent service (run from `out/`, project
`ordo`):
```
docker compose -p ordo restart agent          # for Discord
docker compose -p ordo restart mcp-gateway    # for GitHub PAT
docker compose -p ordo restart comfyui        # for HF
```

## Rotate internal tokens

Internal tokens (`LITELLM_MASTER_KEY`, `OPS_CONTROLLER_TOKEN`,
`OAUTH2_PROXY_COOKIE_SECRET`) live inside `secrets/.env.sops`. (The dashboard
has no per-service auth token in this deployment — the Caddy edge
(oauth2-proxy + Google SSO) is the sole auth gate for every UI, including the
dashboard; `DASHBOARD_AUTH_TOKEN` is not set and is not a secret to rotate
here.) Rotate the rest at once:

```
scripts/secrets/rotate-internal.sh          # re-encrypts secrets/.env.sops
# copy the rotated values into out/secrets.env (the file compose reads)
cd out
docker compose -p ordo restart model-gateway dashboard ops-controller \
    worker agent hermes-dashboard mcp-gateway oauth2-proxy
cd ../..
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
| Civitai | https://civitai.com/user/account → API Keys |

Then on the host:
```
echo -n "$NEW_VALUE" | \
  sops --encrypt --age "$(grep '^# public key:' ~/.config/sops/age/keys.txt | awk '{print $4}')" \
       --input-type=binary --output-type=binary /dev/stdin \
       > secrets/<name>.sops
scripts/secrets/decrypt.sh                          # file-form -> ~/.ai-toolkit/runtime/secrets/
# env-form tokens (e.g. HF, GitHub PAT): also update the matching key in out/secrets.env
docker compose -p ordo restart <consumer-service>    # run from out/
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
| `scripts/secrets/decrypt.sh` fails with `Failed to get the data key` | `SOPS_AGE_KEY_FILE` env var not set or key file unreadable | Set `SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/keys.txt` and verify `chmod 600` |
| `Error unmarshalling input json: invalid character` on .env.sops decrypt | SOPS 3.7.x doesn't auto-detect dotenv format | Use `--input-type=dotenv --output-type=dotenv` flags. The decrypt script already does this. |
| Container starts but immediately exits with `cookie_secret must be 16, 24, or 32 bytes` | `OAUTH2_PROXY_COOKIE_SECRET` was generated with `openssl rand -base64 32` (44 chars) | Regenerate with `LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom \| head -c 32`, edit `secrets/.env.sops`, restart |
| Hermes can't reach Discord but token "looks right" | Bridge from `_FILE` to env var didn't run | Confirm `services/hermes/entrypoint.sh` sources the bridge BEFORE calling the Discord SDK, and that the secret file at `/run/secrets/discord_token` exists in the container |
| `docker compose up` fails with a missing bind-mount source for `.../discord_token` or `.../github_backup_pat` | `~/.ai-toolkit/runtime/secrets/` isn't populated (`out/docker-compose.yml` mounts file-form tokens from there via `OPERATOR_SECRETS_DIR`) | Run `scripts/secrets/decrypt.sh` first to populate `~/.ai-toolkit/runtime/secrets/` |
