# secrets/

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
- `hf_token.sops` — HuggingFace token (gated model downloads). Mounted
  on `ops-controller`, `dashboard`, `gguf-puller`, and the comfyui
  model puller.
- `tavily_key.sops` — Tavily search MCP key. Mounted on `mcp-gateway`.
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
  `docker compose --env-file ~/.ai-toolkit/runtime/.env up -d`).
- Add a new secret: `echo -n "$VALUE" | sops --encrypt --age age1...
  --input-type=binary --output-type=binary /dev/stdin >
  secrets/<name>.sops`.

See `docs/runbooks/secrets.md` for the full lifecycle, recovery
procedures, and rotation runbooks.
