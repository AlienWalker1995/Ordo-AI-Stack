# Secrets - Operator Runbook

## Mental model

- **One store.** Every secret value lives in one SOPS (age) encrypted dotenv
  file in a **private** repo, named by `site: SECRETS_SOURCE` in your
  `ordo.yaml`. The documented place is a private repo checked out beside this
  one: `SECRETS_SOURCE: ../ordo-personal/secrets/ordo.env.sops` (a relative
  path resolves against this checkout; any path works).
- **Everything else is materialized.** `ordo secrets materialize` decrypts the
  store in memory and writes, mode 600:
  - `out/secrets.env`: exactly the keys the current render needs
    (`required_secrets` in `out/manifest.json`), plus optional ones the store
    holds. Compose interpolates each service's own keys from it
    (`--env-file secrets.env`); no service loads it whole.
  - `out/secrets/<file>`: the agent's file-form secrets (`secret_files:` in
    `services/hermes/agent.yaml`: `DISCORD_BOT_TOKEN` -> `discord_token`,
    `GITHUB_BACKUP_PAT` -> `github_backup_pat`), bind-mounted read-only at
    `/run/secrets/<file>` so they stay out of `docker inspect`. A key the store
    lacks materializes as an empty file (the entrypoint treats it as unset).
- **One thing to safeguard:** the age private key,
  `~/.config/sops/age/keys.txt` (`SOPS_AGE_KEY_FILE` overrides it). Without it
  the store does not decrypt.
- **No private repo?** A fresh local install (`ordo init` with no
  `--secrets-source`) keeps today's behaviour: `out/secrets.env` itself is the
  store. `ordo secrets list` says which one you have. Every command below
  works the same on either.
- **Optional: Infisical as the source.** With `site: SECRETS_BACKEND:
  infisical` the values come from a (self-hosted) Infisical project
  environment instead, and the SOPS file becomes its offline backup. See
  "Backend: Infisical" below. The materialize contract is the same.
- `out/` is inside the checkout the `agent` container mirror-mounts, so treat
  `out/secrets.env` and `out/secrets/` like any working-copy secret. Hermes
  already holds the file-form tokens as env vars (its entrypoint bridges them).

`ops-controller` mounts `out/` at `/config` and passes both `--env-file
/config/.env` and `--env-file /config/secrets.env` on every compose call, so a
service it recreates comes up with the materialized values. It never holds the
age key and never materializes: decryption is a host-only step.

> **Never paste secrets or the age key into chat, a log, or an issue, and never
> "fix" a secret-stripped service by writing placeholder values.** A
> `missing setting` error from a secret service means the store lacks the key:
> `ordo secrets list` names it, `ordo secrets set KEY --from-stdin` adds it.

## Commands

Run from the repo root. Each takes `--out DIR` (default `out`) and `--source`
(default `<out>/ordo.yaml`). None prints a value.

| Command | What it does |
|---|---|
| `ordo secrets list` | The backend and store, then every key name: `set` / `blank` / `absent`, and whether the render needs it (required, optional, file secret, unused). With Infisical and a backup, also whether each value is `backed up`, `stale in backup` or `not in backup`. |
| `ordo secrets materialize [--from FILE]` | Writes `out/secrets.env` and `out/secrets/*` from the store. Fails naming the required keys with no value, and writes nothing. Refuses to drop a value that is in `out/secrets.env` but not in the store (run `import` first). `--from` reads another SOPS file. |
| `ordo secrets set KEY --from-stdin` | Sets one key from stdin (never argv), materializes, prints the `ordo recreate` that applies it. |
| `ordo secrets set KEY --generate` | Mints an internal secret. Refused for an issued key (HF, GitHub, Google, Tailscale). On a key that already has a value it is a rotation, so the rotation rules below apply. |
| `ordo secrets rotate KEY...` / `--internal` | Fresh generated values, then materialize; prints the store-side steps (`ALTER USER ...`) and the `ordo recreate --reading KEY...` to run. |
| `ordo secrets backup` | Infisical backend only: copies every value of the project environment into the SOPS file (`SECRETS_SOURCE`), adding and updating keys and printing their names. Keys only the SOPS file holds (the identity credentials) are kept. A no-op writes nothing. |
| `ordo secrets import [--from FILE] [--to FILE]` | One-time migration: adds every non-empty value in `out/secrets.env` that the SOPS file lacks (and the agent's file secrets from the retired `OPERATOR_SECRETS_DIR`), sets `site: SECRETS_SOURCE`, removes `OPERATOR_SECRETS_DIR`. A key the store holds with a different value is named and kept (`--overwrite` takes the live one). |

`ordo up` materializes too (after minting the dashboard's local sign-in secret
when the render needs one), so `out/secrets.env` always matches the store at a
bring-up. `ordo init`, `ordo remote enable|disable` and the commands above write
to the store first and then materialize.

SOPS writes go through the file's own recipients (`sops_age__list_*` in its
metadata); a new file is encrypted to `SOPS_AGE_RECIPIENTS`, else to the public
key in your age key file. sops 3.7 has no stdin input on Windows, so a write
encrypts from an owner-only temp file that is deleted before the command
returns; nothing else holds plaintext outside `out/`.

## First-time setup

1. Install: `winget install Mozilla.sops FiloSottile.age` (Windows),
   `brew install sops age` (macOS), or the GitHub releases (Linux).
2. Generate a keypair and back up the `AGE-SECRET-KEY-1...` line to a password
   manager ("Ordo SOPS age key - disaster recovery"):
   ```
   mkdir -p ~/.config/sops/age
   age-keygen -o ~/.config/sops/age/keys.txt
   chmod 600 ~/.config/sops/age/keys.txt
   ```
3. Create the private repo beside this checkout (for example
   `../ordo-personal`) with a `secrets/` folder and a `.gitattributes` line
   `*.sops text eol=lf` (a CRLF checkout breaks sops's metadata parsing).
4. Fresh install: `ordo init --secrets-source ../ordo-personal/secrets/ordo.env.sops`.
   It generates the internal secrets into the SOPS file and materializes
   `out/secrets.env`. Existing install: see "Migrate an existing install".
5. `ordo --source out/ordo.yaml render --out out`, then `ordo up --all`.
6. Commit the `.sops` file in the private repo.

## Migrate an existing install (live `out/secrets.env` -> SOPS)

1. `ordo secrets import` (or `--to PATH` for a non-default location). It
   creates the SOPS file, adds every live value, copies the file secrets from
   `OPERATOR_SECRETS_DIR` when that site key is set, and edits the source.
2. `ordo --source out/ordo.yaml render --out out`.
3. `ordo secrets list`: every required key shows `set`.
4. `ordo secrets materialize`.
5. Deploy the code that moved the agent's file secrets (`ordo build --all`,
   render, `ordo up --all`, outside a GPU lease), so the agent mounts
   `out/secrets/*`.
6. Commit the `.sops` file in the private repo.

A render refuses a leftover `site: OPERATOR_SECRETS_DIR` and names `ordo
secrets import` as the fix.

## Backend: Infisical (optional)

A self-hosted Infisical can be the source of truth instead of the SOPS file.
Ordo reads one project environment (the root folder `/`, shared secrets;
secret imports are not followed) through a **read-only machine identity**
(Universal Auth), and writes the same `out/secrets.env` + `out/secrets/*`.

Site keys (in `ordo.yaml`; the load refuses a bad or half-set combination):

```yaml
site:
  SECRETS_BACKEND: infisical                      # sops | infisical (default: sops with SECRETS_SOURCE)
  INFISICAL_URL: https://infisical.example.lan    # the server's base URL
  INFISICAL_PROJECT: ordo-stack                   # the project slug
  INFISICAL_ENVIRONMENT: prod                     # optional, default prod
  SECRETS_SOURCE: ../ordo-personal/secrets/ordo.env.sops   # optional: the offline backup
```

Identity credentials are never site keys (the load refuses them: site keys
are rendered into `out/.env`). Each is read from an environment variable of
the same name, else from the SOPS file:

| Key | Identity | Needed |
|---|---|---|
| `INFISICAL_ORDO_CLIENT_ID` / `INFISICAL_ORDO_CLIENT_SECRET` | reader (read on the environment, including secret values) | always |
| `INFISICAL_ORDO_WRITER_CLIENT_ID` / `INFISICAL_ORDO_WRITER_CLIENT_SECRET` | writer (create, edit, delete secrets) | optional |

How each command behaves:

- `materialize`, `ordo up`, `list`: read with the reader identity. A missing
  required key fails naming the key. A value in `out/secrets.env` that
  Infisical lacks is refused, as with SOPS (add it to Infisical first).
- `set`, `rotate`, `ordo remote enable|disable`, the dashboard sign-in mint:
  with a writer identity they create, update or delete the changed keys in
  Infisical, then materialize. **Without one they are refused**, naming the
  keys to change in the Infisical UI. They never write the SOPS file instead.
- `set` of one of the four credential keys above writes the SOPS file (they
  unlock Infisical, so they cannot live in it).
- `backup`: copies the project into the SOPS file. Run it after changes in
  Infisical, then commit the `.sops` file. `list` shows keys that drifted.
- `import`: refused (it would write the SOPS file from `out/secrets.env`); use
  `backup`.

Errors name the server and the cause, never a value: `identity credentials
rejected` (HTTP 401: wrong client id or secret), `identity lacks read on
project` (HTTP 403: the identity's project role), `values are hidden` (the
role may list but not read values), `cannot reach Infisical` (network or TLS).
A multi-line value is refused by name (a dotenv line cannot hold it).

### Switch an install from SOPS to Infisical

1. In Infisical: create the project and environment, add every key the SOPS
   file holds (its UI imports a `.env`), and create the read-only machine
   identity (Universal Auth) with read access on that environment.
2. While the backend is still SOPS, store the identity's credentials:
   `ordo secrets set INFISICAL_ORDO_CLIENT_ID --from-stdin`, then the same for
   `INFISICAL_ORDO_CLIENT_SECRET` (and the writer pair, if you made one).
3. Add the site keys above to `ordo.yaml`.
4. `ordo secrets list`: every required key shows `set` and `backed up`.
5. `ordo secrets materialize`: it refuses if Infisical lacks a value the live
   file holds, naming the key.
6. Render and deploy as usual. Nothing in the running stack reads Infisical:
   services still get `out/secrets.env`.

Rolling back is removing `SECRETS_BACKEND` and the `INFISICAL_*` site keys
(after an `ordo secrets backup`), then `ordo secrets materialize`.

## Change a secret

```
ordo secrets set HF_TOKEN --from-stdin < token.txt     # or: pipe it; nothing lands in argv or history
ordo recreate --reading HF_TOKEN                       # what `set` prints; outside a GPU lease
```
A `restart` keeps the old environment; `ordo recreate` builds the new one.
Commit the `.sops` file in the private repo.

## Rotate internal tokens

```
ordo secrets rotate --internal     # or name keys: ordo secrets rotate OPS_CONTROLLER_TOKEN
```
`--internal` rotates every internal token the store holds:
`LITELLM_MASTER_KEY`, `LITELLM_DB_PASSWORD`, every `LITELLM_KEY_*`,
`OPS_CONTROLLER_TOKEN`, `THROUGHPUT_RECORD_TOKEN`, `OAUTH2_PROXY_COOKIE_SECRET`,
`HERMES_API_SERVER_KEY`, and the Langfuse infra credentials
(`LANGFUSE_DB_PASSWORD`, `_CLICKHOUSE_PASSWORD`, `_REDIS_AUTH`,
`_MINIO_SECRET`, `_NEXTAUTH_SECRET`). It never touches:

- `LITELLM_SALT_KEY`, `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY`,
  `LIVESYNC_E2EE_PASSPHRASE`: each hashes or encrypts stored data, so a new
  value makes it unmatchable or unreadable. `rotate` refuses them by name too.
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`: Langfuse stores the pair.
  Rotate it in the Langfuse UI (project settings, API keys), then
  `ordo secrets set` both halves.
- `LANGFUSE_ADMIN_PASSWORD`: it seeds the login only on Langfuse's first boot.
  Change it in the Langfuse UI.

The command prints what must happen before the recreate. Databases keep their
own copy of a password, so run the `ALTER USER` it names first:

- `LITELLM_DB_PASSWORD`: `ALTER USER litellm PASSWORD '<new>';` in
  `docker exec -it ordo-litellm-db-1 psql -U litellm -d litellm`.
- `LANGFUSE_DB_PASSWORD` / `LANGFUSE_CLICKHOUSE_PASSWORD`: the same inside
  `langfuse-db` / `langfuse-clickhouse`.

The new value is in `out/secrets.env` (`grep KEY out/secrets.env` on the host).
Then run the printed `ordo recreate --reading KEY...` outside a GPU lease: it
recreates every service whose rendered definition reads a rotated key
(ops-controller included). A rotated cookie secret ends every oauth2-proxy
session; a rotated `LANGFUSE_NEXTAUTH_SECRET` ends open Langfuse sessions.
The evals one-shot reads its environment on each run, so its next run picks up
the new values.

## Rotate high-value tokens (issuer-side)

Regenerate at the provider first, then store the new value:

| Provider | Where to regenerate | Key |
|---|---|---|
| Discord bot | https://discord.com/developers/applications -> bot -> Reset Token | `DISCORD_BOT_TOKEN` |
| GitHub PAT | https://github.com/settings/tokens (revoke + create) | `GITHUB_PERSONAL_ACCESS_TOKEN`, `GITHUB_BACKUP_PAT` |
| HuggingFace | https://huggingface.co/settings/tokens | `HF_TOKEN` |
| Tailscale | admin console -> Settings -> Keys | `TS_AUTHKEY` |

```
ordo secrets set <KEY> --from-stdin      # then run the recreate it prints
```

## Recovery - age key lost

Restore the private key from your password-manager backup. Without it the store
does not decrypt. The stack is recoverable (regenerate every token at its
provider, `ordo secrets import` from a working `out/secrets.env` into a new
file) but the generate-once salts are gone with it: back up the key.

## Recovery - age key leaked

Treat as catastrophic:

1. Generate a new keypair: `age-keygen -o ~/.config/sops/age/keys.txt.new`.
2. Re-encrypt the store to the new recipient (`sops updatekeys` after editing
   the private repo's `.sops.yaml`, or `SOPS_AGE_RECIPIENTS=<new> ordo secrets
   import --to <new file>` from a materialized `out/secrets.env`).
3. **Rotate every token at its provider** and `ordo secrets rotate --internal`:
   the old ciphertext stays decryptable by anyone with the leaked key.
4. Run `scripts/secrets/audit-git-history.sh` to confirm this public repo holds
   no plaintext.

## The committed `secrets/*.sops` blobs

`secrets/` in this public repo still holds older encrypted blobs
(`.env.sops`, `discord_token.sops`, ...). Nothing reads them: they are not the
source of any value the stack runs with. Whether to delete them is a separate
decision; do not edit them.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `sops --decrypt failed ... Failed to get the data key` | `SOPS_AGE_KEY_FILE` unset or the key unreadable | Set `SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/keys.txt`; `chmod 600` it |
| `parsing time ... extra text: "\x0d"` | The `.sops` file was checked out with CRLF | `*.sops text eol=lf` in the private repo's `.gitattributes`, then re-checkout |
| `materialize` refuses: `out/secrets.env has value(s) that ... lacks` | A value exists only in the live file | `ordo secrets import`, then materialize again |
| `required secret(s) with no value` | The store lacks a key the render needs | `ordo secrets set KEY --from-stdin` (an internal one: `--generate`) |
| Container exits with `cookie_secret must be 16, 24, or 32 bytes` | A hand-made `OAUTH2_PROXY_COOKIE_SECRET` | `ordo secrets rotate OAUTH2_PROXY_COOKIE_SECRET`, then the printed recreate |
| The agent's Discord or backup token is missing | `out/secrets/<file>` is empty | `ordo secrets set DISCORD_BOT_TOKEN --from-stdin`, then `ordo recreate agent` |
