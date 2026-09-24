# Secrets — Operator Runbook

## Mental model

- **One thing to safeguard**: `~/.config/sops/age/keys.txt` (your age
  private key). Everything else is encrypted at rest in `secrets/*.sops`,
  safe to commit to a public repo.
- Two delivery forms decrypt only when needed:
  - **Env-form** → the gitignored `out/secrets.env`, from which compose
    interpolates each service's own secrets (no service loads it whole).
  - **File-form** → `~/.ai-toolkit/runtime/secrets/<name>`, mounted into
    containers as Docker secrets (`/run/secrets/<name>`), so they never
    appear in `docker inspect`.
- `~/.ai-toolkit/runtime/secrets/` is **outside** the container bind mounts,
  so file-form tokens are not readable as plain files from inside a
  container. This is not a hard boundary for Hermes: the `agent` service
  mounts `/var/run/docker.sock` (see
  [bounded-hermes.md](bounded-hermes.md)). `out/secrets.env`
  **is** inside the repo working tree the `agent` container mounts — treat
  it with the same care as any working-copy file.

## How services receive secrets at runtime

- **Env-form** (`secrets/.env.sops` → `out/secrets.env`): `ordo render`
  emits `out/secrets.env.example` (keys only, values empty). Copy it to
  `out/secrets.env` and fill real values — by hand or via
  `sops --decrypt --input-type=dotenv --output-type=dotenv
  secrets/.env.sops`. No service loads `secrets.env` as an `env_file`:
  each service's manifest lists the secret names it reads (`secrets:` on a
  plugin service, the agent and dashboard manifests, or the core service's
  list in `ordo/compose.py`), the renderer emits `KEY: ${KEY}` into that
  service's `environment:`, and compose interpolates the value from
  `--env-file secrets.env`. A service therefore holds only the secrets it
  reads (`tests/substrate/test_secret_scoping.py` pins this). `ordo up` /
  `ordo recreate` always pass both env files, so the values arrive
  (`OAUTH2_PROXY_*`, `OPS_CONTROLLER_TOKEN`, `LITELLM_MASTER_KEY`,
  `SEARXNG_SECRET`, `N8N_API_KEY`, …). A hand-assembled compose command
  without `--env-file secrets.env` interpolates every secret empty.
- **File-form** (`secrets/<name>.sops` → `runtime/secrets/<name>`): mounted
  as Docker secrets at `/run/secrets/<name>`. Where an app SDK expects a
  plain env var, the consumer's entrypoint **bridges** `<NAME>_FILE` → a
  `<NAME>` env var — so the app reads the token from its environment and
  never needs a plaintext secret in `.env`.

**`ops-controller` recreates carry secrets forward.** `ops-controller`
mounts `out/` at `/config`, and every compose call it makes passes both
`--env-file /config/.env` and `--env-file /config/secrets.env`
(`DockerBackend._compose` in `ordo/broker.py`). So a secret-dependent
service it recreates (oauth2-proxy, caddy, searxng, n8n, dashboard,
model-gateway) comes up with real values rather than crash-looping on
placeholders. `ops-controller` never holds the age key: decrypting `.sops`
blobs stays a host-only operation.

> **Never paste secrets or the age key into chat, a log, or an issue, and
> never "fix" a secret-stripped service by writing placeholder values into
> `.env` or stubbing empty `secrets/*` files.** A `missing setting` /
> `not a directory` error from a secret service means `out/secrets.env`
> (or, for file-form tokens, `~/.ai-toolkit/runtime/secrets/`) wasn't
> populated — the fix is filling it on the host, not a synthesized value.

## First-time setup

1. Install: `winget install Mozilla.sops FiloSottile.age` (Windows),
   `brew install sops age` (macOS), or the GitHub releases (Linux).
2. Generate a keypair:
   ```
   mkdir -p ~/.config/sops/age
   age-keygen -o ~/.config/sops/age/keys.txt
   chmod 600 ~/.config/sops/age/keys.txt
   ```
3. Back up the private key line (`AGE-SECRET-KEY-1...`) to a password
   manager under "Ordo SOPS age key — disaster recovery."
4. Paste the public key line (`age1...`) into `secrets/.sops.yaml` under
   `creation_rules.[*].age`. The public key is safe to commit.
5. Render (`python -m ordo --source out/ordo.yaml render --out out`), copy
   `out/secrets.env.example` → `out/secrets.env`, fill real values, then
   `ordo up --all` from the repo root.

## Edit a secret

```
sops secrets/.env.sops              # opens decrypted in $EDITOR, re-encrypts on save
sops secrets/<name>.sops            # same for individual file-form tokens
```
For env-form keys, also copy the changed value into `out/secrets.env` —
that's the file compose reads; re-encrypting `.env.sops` alone doesn't
propagate. Then recreate each service that reads the key, from the repo root (a `restart`
keeps the old environment), for example `ordo recreate agent`. To find the readers, grep
`out/docker-compose.yml` for `${KEY}`.

## Rotate internal tokens

Internal tokens (`LITELLM_MASTER_KEY`, `LITELLM_DB_PASSWORD`, every
`LITELLM_KEY_*`, `OPS_CONTROLLER_TOKEN`, `OAUTH2_PROXY_COOKIE_SECRET`) live in
`secrets/.env.sops`. `LITELLM_SALT_KEY` is deliberately excluded: rotating it
makes every credential LiteLLM stored in Postgres unreadable. Rotate the rest at
once:
```
scripts/secrets/rotate-internal.sh          # re-encrypts secrets/.env.sops
# copy the rotated values into out/secrets.env, then run the
# `ordo recreate --reading <rotated keys>` the script prints, outside a GPU lease:
# it recreates every service whose rendered definition reads a rotated key
# (ops-controller included, which `ordo recreate` refuses while the card is leased)
git add secrets/.env.sops && git commit -m "chore(secrets): rotate internal tokens" && git push
```
The cookie-secret rotation invalidates every oauth2-proxy session.

`LITELLM_DB_PASSWORD` needs one extra step before the recreate above: the new
password only works once Postgres itself has it, so run
`ALTER USER litellm PASSWORD '<new value>';` inside the `litellm-db` container
(`docker exec -it ordo-litellm-db-1 psql -U litellm -d litellm`) before
copying the rotated value into `out/secrets.env` and recreating
`model-gateway`.

## Rotate high-value tokens (issuer-side)

Regenerate at the provider first, then re-encrypt the new value:

| Provider | Where to regenerate |
|---|---|
| Discord bot | https://discord.com/developers/applications → bot → Reset Token |
| GitHub PAT | https://github.com/settings/tokens (revoke + create) |
| HuggingFace | https://huggingface.co/settings/tokens |
| Civitai | https://civitai.com/user/account → API Keys |

```
echo -n "$NEW_VALUE" | \
  sops --encrypt --age "$(grep '^# public key:' ~/.config/sops/age/keys.txt | awk '{print $4}')" \
       --input-type=binary --output-type=binary /dev/stdin \
       > secrets/<name>.sops
scripts/secrets/decrypt.sh                          # file-form -> ~/.ai-toolkit/runtime/secrets/
# env-form tokens (e.g. HF, GitHub PAT): also update the matching key in out/secrets.env
ordo recreate <consumer-service>                    # from the repo root
git add secrets/<name>.sops && git commit && git push
```

## Recovery — age key lost

Restore the private key from your password-manager backup. Without it, none
of `secrets/*.sops` decrypts. The repo is recoverable (regenerate every
token at its provider, re-encrypt with a new key) but painful — back up the
key.

## Recovery — age key leaked

Treat as catastrophic:

1. Generate a new keypair: `age-keygen -o ~/.config/sops/age/keys.txt.new`.
2. Update `secrets/.sops.yaml` with the new public key.
3. For each `secrets/*.sops`: decrypt with the old key, re-encrypt with the new.
4. Force-push `secrets/`.
5. **Rotate every actual token at its provider** — the old blobs stay
   forever-decryptable by anyone with the leaked key, even after force-push.
6. Run `scripts/secrets/audit-git-history.sh` to confirm a clean state.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `decrypt.sh` fails with `Failed to get the data key` | `SOPS_AGE_KEY_FILE` unset or key unreadable | Set `SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/keys.txt`; verify `chmod 600` |
| `invalid character` on `.env.sops` decrypt | SOPS doesn't auto-detect dotenv | Pass `--input-type=dotenv --output-type=dotenv` (the decrypt script already does) |
| Container exits with `cookie_secret must be 16, 24, or 32 bytes` | `OAUTH2_PROXY_COOKIE_SECRET` made with `openssl rand -base64 32` (44 chars) | Regenerate with `tr -dc 'a-zA-Z0-9' </dev/urandom \| head -c 32`, edit `secrets/.env.sops`, copy it into `out/secrets.env`, `ordo recreate oauth2-proxy` |
| App can't reach a provider but the token "looks right" | `_FILE`→env-var bridge didn't run | Confirm the service entrypoint sources the bridge before calling the SDK, and `/run/secrets/<name>` exists in the container |
| `ordo up` fails on a missing bind-mount source under `runtime/secrets/` | `~/.ai-toolkit/runtime/secrets/` not populated | Run `scripts/secrets/decrypt.sh` first |
