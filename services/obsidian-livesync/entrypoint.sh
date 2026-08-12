#!/bin/sh
# ordo/livesync-bridge entrypoint.
#
# 1. Wait for CouchDB and ensure its system + notes databases exist (idempotent - the official
#    image does not create them reliably when COUCHDB_USER is set).
# 2. Render the literal dat/config.json from env (secrets never live in a tracked file).
# 3. Exec the headless sync daemon.
set -eu

: "${COUCHDB_INTERNAL_URL:?COUCHDB_INTERNAL_URL must be set}"
: "${COUCHDB_USER:?COUCHDB_USER must be set}"
: "${COUCHDB_PASSWORD:?COUCHDB_PASSWORD must be set}"
: "${LIVESYNC_DATABASE:?LIVESYNC_DATABASE must be set}"
PASSPHRASE="${LIVESYNC_E2EE_PASSPHRASE:-}"

echo "livesync-bridge: waiting for CouchDB at ${COUCHDB_INTERNAL_URL} ..."
i=0
until curl -fsS -u "${COUCHDB_USER}:${COUCHDB_PASSWORD}" "${COUCHDB_INTERNAL_URL}/_up" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 150 ]; then
        echo "livesync-bridge: CouchDB did not become ready within ~5m - giving up" >&2
        exit 1
    fi
    sleep 2
done

# Configure CouchDB via the _config API rather than a bind-mounted ini. The couchdb image's
# entrypoint runs `find ! -user couchdb -exec chown -f couchdb:couchdb` under `set -e`, which
# fails on a root-owned bind mount (Docker Desktop can't chown binds) and silently kills the
# container. The API is owned by couchdb, persisted, and idempotent. Requires an admin (created
# from COUCHDB_USER/PASSWORD), which the wait-loop above already confirmed.
cfg() {  # cfg <section> <key> <value>
    curl -fsS -X PUT -u "${COUCHDB_USER}:${COUCHDB_PASSWORD}" \
        "${COUCHDB_INTERNAL_URL}/_node/_local/_config/$1/$2" \
        -H 'Content-Type: application/json' -d "\"$3\"" >/dev/null 2>&1 || true
}
# CORS so the Obsidian desktop (app://obsidian.md) + mobile (capacitor://localhost) webviews can
# authenticate. LiveSync's config checker looks for enable_cors under [chttpd] (3.x's HTTP daemon)
# as well as [httpd] — set both. WWW-Authenticate makes CouchDB return a proper Basic auth
# challenge (LiveSync flags its absence). The size bumps are LiveSync's recommended values — all
# four are exactly what LiveSync's "Fix" button would apply.
cfg httpd enable_cors true
cfg chttpd enable_cors true
cfg cors origins "app://obsidian.md,capacitor://localhost,http://localhost"
cfg cors credentials true
cfg cors methods "GET, PUT, POST, HEAD, DELETE"
cfg cors headers "accept, authorization, content-type, origin, referer"
cfg chttpd max_http_request_size 4294967296
cfg couchdb max_document_size 50000000
# WWW-Authenticate carries embedded quotes, which cfg()'s simple wrapper can't JSON-encode — PUT
# it directly as pre-escaped JSON.
curl -fsS -X PUT -u "${COUCHDB_USER}:${COUCHDB_PASSWORD}" \
    "${COUCHDB_INTERNAL_URL}/_node/_local/_config/httpd/WWW-Authenticate" \
    -H 'Content-Type: application/json' -d '"Basic realm=\"couchdb\""' >/dev/null 2>&1 || true
# Close the server to anonymous access (matters: this endpoint is reachable off-tailnet via
# Funnel). Exempt /_up so healthchecks still work. Set LAST + as admin, so we can't lock ourselves
# out mid-config.
cfg chttpd require_valid_user_except_for_up true
cfg chttpd require_valid_user true

# Idempotent DB creation: 201 created / 412 already-exists are both fine (|| true). system dbs +
# the LiveSync notes db (the official image doesn't create these reliably when COUCHDB_USER is set).
for db in _users _replicator _global_changes "${LIVESYNC_DATABASE}"; do
    curl -fsS -X PUT -u "${COUCHDB_USER}:${COUCHDB_PASSWORD}" \
        "${COUCHDB_INTERNAL_URL}/${db}" >/dev/null 2>&1 || true
done

# Storage-mount readiness gate (data-loss guard). The storage peer materializes CouchDB into the
# notes/ folder, which on Docker Desktop is a 9p bind that populates LAZILY: for the first seconds
# after container start the folder enumerates incrementally, so any startup scan that runs against
# it sees a PARTIAL tree and misreads the not-yet-visible files as deletions. Combined with the
# bridge's from-scratch (in-memory) index, that manufactured a mass-delete storm that ate ~1.5k
# vault files and re-uploaded the rest. Guard: when CouchDB is populated, do not start the sync
# daemon until the on-disk file count has STABILIZED (unchanged across 3 consecutive 5s samples),
# i.e. the 9p mount has finished enumerating. If it never stabilizes, exit rather than scan a
# partial tree (under restart: unless-stopped this re-checks until the mount is fully ready).
# Escape hatch for a deliberate fresh-device re-materialization: when an operator has
# intentionally emptied notes/ to re-download the whole vault from CouchDB, the empty mount is
# expected, not a not-ready 9p bind. LSB_ALLOW_EMPTY_STORAGE=1 skips the gate for that one run.
COUCH_DOCS=$(curl -fsS -u "${COUCHDB_USER}:${COUCHDB_PASSWORD}" \
    "${COUCHDB_INTERNAL_URL}/${LIVESYNC_DATABASE}" 2>/dev/null \
    | grep -oE '"doc_count":[0-9]+' | cut -d: -f2)
if [ "${LSB_ALLOW_EMPTY_STORAGE:-0}" = "1" ]; then
    echo "livesync-bridge: LSB_ALLOW_EMPTY_STORAGE=1 - skipping the mount-readiness gate (deliberate re-materialization)."
elif [ "${COUCH_DOCS:-0}" -gt 100 ]; then
    prev=-1; stable=0; k=0
    while [ "$stable" -lt 3 ]; do
        n=$(find /app/data/notes -type f 2>/dev/null | wc -l)
        if [ "$n" -gt 0 ] && [ "$n" -eq "$prev" ]; then
            stable=$((stable + 1))
        else
            stable=0
        fi
        prev="$n"
        k=$((k + 1))
        if [ "$k" -gt 60 ]; then
            echo "livesync-bridge: FATAL - notes/ file count not stabilizing (last=${n}, CouchDB has ${COUCH_DOCS} docs) after ~5m. 9p mount not fully ready; refusing to start to avoid a partial-scan delete storm." >&2
            exit 1
        fi
        echo "livesync-bridge: waiting for notes/ 9p mount to finish populating (count=${n}, stable=${stable}/3)..."
        sleep 5
    done
    echo "livesync-bridge: notes/ mount stabilized at ${prev} files; safe to start sync."
fi

# Render dat/config.json. The couchdb peer and the storage peer share group "notes", which is how
# the bridge knows to mirror them. baseDir "" on the couchdb side = the whole LiveSync vault;
# "data/notes/" on the storage side = /app/data/notes (the bind-mounted vault notes/ folder).
# scanOfflineChanges is FALSE: the offline-changes reconciliation diffs a fresh in-memory index
# against the disk and propagates the result as deletions — catastrophic against a lazily-mounted
# 9p bind (it deleted ~1.5k files on a restart). Live sync does NOT need it: useChokidar (polling,
# CHOKIDAR_USEPOLLING) carries host/AI edits -> CouchDB in ~3s, and device -> CouchDB -> file is
# network-driven; only changes made while the bridge was DOWN are missed, and they re-sync on the
# next touch — a benign staleness, never a deletion.
# Generated secrets are base64url (JSON-safe: no " or \), so heredoc expansion can't break the JSON.
mkdir -p /app/dat /app/data/notes
cat > /app/dat/config.json <<JSON
{
  "peers": [
    {
      "type": "couchdb",
      "name": "ordo-notes-couchdb",
      "group": "notes",
      "database": "${LIVESYNC_DATABASE}",
      "url": "${COUCHDB_INTERNAL_URL}",
      "username": "${COUCHDB_USER}",
      "password": "${COUCHDB_PASSWORD}",
      "passphrase": "${PASSPHRASE}",
      "baseDir": ""
    },
    {
      "type": "storage",
      "name": "ordo-notes-storage",
      "group": "notes",
      "baseDir": "data/notes/",
      "scanOfflineChanges": false,
      "useChokidar": true
    }
  ]
}
JSON

echo "livesync-bridge: config written; starting sync daemon (deno task run)."
exec deno task run
