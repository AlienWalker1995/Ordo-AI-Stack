# secrets/

> **Note.** The SOPS + age **at-rest** model here (encrypted `*.sops` blobs, safe to commit) is unchanged. Runtime materialization is owned by the render substrate: `ordo render` writes a keys-only `out/secrets.env.example`; the operator fills real values into a gitignored **`out/secrets.env`** (SOPS-decrypt or hand-set) that the rendered compose reads as a second `env_file`. The old V1 `make decrypt-secrets` → `~/.ai-toolkit/runtime/` + `make up` two-`--env-file` flow was removed along with the rest of the V1 tree (2026-07-24, commit `62540bf`). See [`../docs/history/CUTOVER.md`](../docs/history/CUTOVER.md) (Secrets) and [`../docs/LEGACY-CLEANUP.md`](../docs/LEGACY-CLEANUP.md).

Encrypted-at-rest secrets for the Ordo AI stack. **All `*.sops` files in
this directory are safe to commit to a public repo** — they decrypt only
with the age private key at `~/.config/sops/age/keys.txt`.

## Inventory

- `.sops.yaml` — SOPS recipient config (your age public key only).
- `.env.sops` — env-form internal tokens (`LITELLM_MASTER_KEY`,
  `OPS_CONTROLLER_TOKEN`, `OAUTH2_PROXY_CLIENT_ID`,
  `OAUTH2_PROXY_CLIENT_SECRET`, `OAUTH2_PROXY_COOKIE_SECRET`).
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
- Decrypt for runtime: `ordo render` (run from the repo root) writes
  `out/secrets.env.example` — the secret KEYS the enabled stack needs,
  values empty. Copy it to `out/secrets.env` and fill in real values
  (SOPS-decrypt the relevant `secrets/<name>.sops` file, or hand-set).
  `out/secrets.env` is gitignored, never committed.
- Bring up the stack: from `out/`, `docker compose -p ordo up -d`
  (the rendered compose reads `secrets.env` as a second, optional
  `env_file` layered after `.env`, so derived config and operator
  secrets stay in separate files).
- The dashboard's per-service recreate (backend `ops-api`) replays the
  rendered `out/` tree — both `.env` and `secrets.env` — so a
  secret-dependent service it recreates comes up with real values. It
  never holds the age key. See `docs/runbooks/secrets.md`.
- Add a new secret: `echo -n "$VALUE" | sops --encrypt --age age1...
  --input-type=binary --output-type=binary /dev/stdin >
  secrets/<name>.sops`.

See `docs/runbooks/secrets.md` for the full lifecycle, recovery
procedures, and rotation runbooks.
