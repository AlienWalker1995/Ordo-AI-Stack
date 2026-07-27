# Runbook: cross-device Obsidian notes sync (CouchDB LiveSync)

Sync one Obsidian vault across iOS / Mac / PC, reachable from any tailscaled device (and,
optionally, off-tailnet), and readable/writable by the AI stack.

## How it works

```
iOS / Mac / PC  --Self-hosted LiveSync plugin-->  CouchDB  <--livesync-bridge-->  data/memory-vault/notes/
 (native Obsidian, real-time)                     (the URL)   (headless, 24/7)       |
                                                                                     +- memory-vault MCP (RW) - Hermes
 off-tailnet --Tailscale Funnel--> notes.<tailnet> --> CouchDB                        +- rag-ingestion (RO) -> qdrant_search
```

- **CouchDB** is the sync server the Obsidian LiveSync plugin talks to. Reached through the edge at
  `https://<CADDY_TAILNET_HOSTNAME>/couchdb` (CouchDB's own auth — a non-browser sync client can't
  pass Google SSO, so this route bypasses SSO like `/llm` and `/mcp`).
- **`livesync-bridge`** mirrors the CouchDB database to plain `.md` files under
  `data/memory-vault/notes/`, so the AI sees your notes with no desktop app running.
- Human notes live in `notes/`; the AI's own memory stays in `memories/`. RAG indexes the whole
  vault, so `qdrant_search` finds notes automatically and the memory-vault MCP can read them.

## 1. Enable it on the stack

`ordo init` offers a **Notes sync** capability; keep it enabled and it mints `COUCHDB_PASSWORD` +
`LIVESYNC_E2EE_PASSPHRASE` into `out/secrets.env`. Then:

```bash
# create the vault notes/ folder (uid-1000-clean) + the couchdb data dir
./scripts/ensure_dirs.sh                 # (or scripts/ensure_dirs.ps1 on Windows)
# build the bridge image (from pinned source) and render
docker build -t ordo/livesync-bridge:latest services/obsidian-livesync
ordo --source out/ordo.yaml render --out out
# bring it up (add `notes` to your existing profiles)
cd out && COMPOSE_PROFILES=<your-profiles>,notes docker compose -p ordo \
  --env-file .env --env-file secrets.env up -d couchdb livesync-bridge
```

Verify: `docker logs ordo-livesync-bridge-1` shows `Database is now ready` and
`Scan offline changes: Enabled`. A note dropped in `data/memory-vault/notes/` reaches CouchDB
within ~3s (polling; Docker Desktop bind mounts emit no inotify events).

## 2. Set up each Obsidian device (iOS / Mac / PC)

The vault on each device is always a **local folder** — the server URL goes in the **plugin**, not
Obsidian's open-vault screen.

1. Obsidian -> open/create a local vault.
2. **Settings -> Community plugins** -> turn off Restricted Mode -> **Browse** -> install
   **Self-hosted LiveSync** -> enable it.
3. On your **first** device, run the LiveSync setup wizard:
   - **URI**: `https://<CADDY_TAILNET_HOSTNAME>/couchdb`  (tailnet) — see step 4 for off-tailnet.
   - **Username / Password**: `COUCHDB_USER` (default `ordoadmin`) / `COUCHDB_PASSWORD` from
     `out/secrets.env`.
   - **Database**: `obsidian-notes`.
   - **End-to-end encryption**: **On**, passphrase = `LIVESYNC_E2EE_PASSPHRASE` from
     `out/secrets.env`. **Leave "Path Obfuscation" OFF** — the bridge writes readable filenames.
   - Let it replicate, then **Copy setup URI**.
4. On every **other** device: install the plugin, choose "Setup from another device", and
   **paste the setup URI** (or scan its QR). Done — all settings carry over.

The E2EE passphrase must be **identical** on every client and matches the one the bridge uses
(from `secrets.env`), or content won't decrypt.

## 3. Off-tailnet access (Tailscale Funnel) — optional, opt-in

Makes the endpoint reachable from devices with **no Tailscale**. Note content is still
end-to-end encrypted at rest in CouchDB, and `require_valid_user` gates all access.

**One-time operator prerequisite** — grant the `notes` node the Funnel attribute in your tailnet
ACL (admin console -> Access controls):

```jsonc
"nodeAttrs": [
  { "target": ["tag:ordo-edge"], "attr": ["funnel"] }
]
```

Then bring up the opt-in profile (separate from `notes` — public exposure is a deliberate flip):

```bash
cd out && COMPOSE_PROFILES=<your-profiles>,notes,notes-funnel docker compose -p ordo \
  --env-file .env --env-file secrets.env up -d notes-funnel
docker exec ordo-notes-funnel-1 tailscale funnel status    # confirm it's public
```

Off-tailnet LiveSync **URI**: `https://notes.<CADDY_TAILNET_DOMAIN>/` (same username / password /
database / passphrase). On-tailnet clients can keep using `.../couchdb`.

## Troubleshooting

- **Bridge stuck "waiting for CouchDB"** — CouchDB isn't up/healthy yet, or `COUCHDB_PASSWORD`
  mismatch between `couchdb` and `livesync-bridge` (both read it from `secrets.env`). Check
  `docker logs ordo-couchdb-1`.
- **AI/host-written notes not syncing to devices** — the bridge polls bind mounts every 3s
  (`CHOKIDAR_INTERVAL`); a note pre-existing at bridge start is caught by the offline scan on
  (re)start. Live host writes need `CHOKIDAR_USEPOLLING=1` (default on).
- **Mobile can't connect** — CORS. The bridge sets the required origins
  (`app://obsidian.md`, `capacitor://localhost`) via the `_config` API at startup; confirm with
  `docker exec ordo-couchdb-1 curl -s -u <user>:<pw> localhost:5984/_node/_local/_config/cors`.
- **Reset a device** — LiveSync's "rebuild" fetches the whole vault fresh from CouchDB.
- **Never** expose CouchDB's Fauxton admin UI publicly — the edge blocks `/couchdb/_utils`; the
  Funnel path proxies CouchDB directly, so rely on `require_valid_user` there.
