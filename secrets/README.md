# secrets/

> ⚠️ **Partially LEGACY — reconcile with v2/ (cutover 2026-07-09).** The SOPS + age **at-rest** model here (encrypted `*.sops` blobs, safe to commit) is unchanged. What changed is the **runtime materialization**: in the production **v2** stack, `ordo render` writes a keys-only `v2/out/secrets.env.example`; the operator fills real values into a gitignored **`v2/out/secrets.env`** (SOPS-decrypt or hand-set) that the rendered compose reads as a second `env_file`. The V1 `make decrypt-secrets` → `~/.ai-toolkit/runtime/` + `make up` two-`--env-file` flow described below is legacy. See [`../v2/CUTOVER.md`](../v2/CUTOVER.md) (Secrets) and [`../docs/LEGACY-CLEANUP.md`](../docs/LEGACY-CLEANUP.md).

Encrypted-at-rest secrets for the Ordo AI stack. **All `*.sops` files in
this directory are safe to commit to a public repo** — they decrypt only
with the age private key at `~/.config/sops/age/keys.txt`.

## Inventory

- `.sops.yaml` — SOPS recipient config (your age public key only).
- `.env.sops` — env-form internal tokens (`LITELLM_MASTER_KEY`,
  `DASHBOARD_AUTH_TOKEN`, `OPS_CONTROLLER_TOKEN`,
  `OAUTH2_PROXY_CLIENT_ID`, `OAUTH2_PROXY_CLIENT_SECRET`,
  `OAUTH2_PROXY_COOKIE_SECRET`).
- `discord_token.sops` — Discord bot token. Mounted as
  `/run/secrets/discord_token` on `hermes-gateway`.
- `github_pat.sops` — GitHub fine-grained PAT. Mounted on
  `mcp-gateway` and `comfyui` (the latter as `GITHUB_TOKEN_FILE` for
  ComfyUI-Manager).
- `github_backup_pat.sops` — classic GitHub PAT for `git push` to the
  `ordo-hermes-backup` private repo. Mounted on `hermes-gateway`; the
  entrypoint bridges it to the `GITHUB_BACKUP_PAT` env var, and the backup
  repo's credential helper reads it. Not used by the stack services themselves.
- `hf_token.sops` — HuggingFace token (gated model downloads). Mounted
  on `ops-controller`, `dashboard`, `gguf-puller`, and the comfyui
  model puller.
- `civitai_token.sops` — Civitai token (LoRA downloads). Mounted on
  the comfyui model puller.

## Working with these files

- Edit: `sops secrets/<file>.sops` opens decrypted in `$EDITOR`,
  re-encrypts on save.
- Decrypt for runtime: `make decrypt-secrets` writes plaintext to
  `~/.ai-toolkit/runtime/`. The runtime dir is outside `/workspace`
  and the `HERMES_HOST_DEV_MOUNT`, so even a prompt-injected Hermes
  cannot `cat` the decrypted files.
- Bring up the stack: `make up` (runs decrypt-secrets, then
  `docker compose --env-file .env --env-file ~/.ai-toolkit/runtime/.env up -d`
  — two files, last-wins, so `.env` defaults are kept and runtime secrets win).
- `ops-controller` mounts `runtime/.env` read-only and injects it when it
  recreates secret-dependent services, so dashboard-driven recreate brings them
  up with real values. It never holds the age key. See
  `docs/runbooks/secrets.md`.
- Add a new secret: `echo -n "$VALUE" | sops --encrypt --age age1...
  --input-type=binary --output-type=binary /dev/stdin >
  secrets/<name>.sops`.

See `docs/runbooks/secrets.md` for the full lifecycle, recovery
procedures, and rotation runbooks.
