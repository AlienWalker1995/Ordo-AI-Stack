# secrets/

**This directory is not the secret store.** The operator's secrets live in one
SOPS (age) encrypted dotenv file in a private repo, named by
`site: SECRETS_SOURCE` in `ordo.yaml` (documented default:
`../ordo-secrets/secrets.env.sops`, beside this checkout). `ordo secrets
materialize` writes `out/secrets.env` and the agent's file secrets
(`out/secrets/*`) from it; nothing is filled in by hand. Without a configured
SOPS file (a fresh local install), `out/secrets.env` itself is the store.

Optionally, a self-hosted Infisical project can be the source instead
(`site: SECRETS_BACKEND: infisical`, with `INFISICAL_URL`, `INFISICAL_PROJECT`
and `INFISICAL_ENVIRONMENT`). The SOPS file then stays as the offline backup
(`ordo secrets backup`) and holds the machine identity's credentials.

The single flow, every command, migration and rotation:
[`docs/runbooks/secrets.md`](../docs/runbooks/secrets.md).

```
ordo secrets list                          # the backend, key names, set/blank, what the render needs
ordo secrets set KEY --from-stdin          # change one value, then run the recreate it prints
ordo secrets rotate --internal             # fresh internal tokens (salts and issued keys refused)
ordo secrets import                        # one-time: live out/secrets.env -> the SOPS file
ordo secrets materialize                   # out/secrets.env + out/secrets/* from the store
ordo secrets backup                        # Infisical backend: copy the project into the SOPS file
```

## What is committed here

- `.sops.yaml`: a SOPS recipient config (an age public key).
- `*.sops` (`.env.sops`, `discord_token.sops`, `github_pat.sops`, ...): older
  encrypted blobs from before the private store. Nothing reads them and they
  are not the source of any value the stack runs with. They stay untouched
  until the operator decides their fate; do not edit them or add new ones.

`scripts/secrets/audit-git-history.sh` checks this public repo's history for
plaintext secrets.
