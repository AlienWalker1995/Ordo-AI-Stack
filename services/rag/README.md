# rag-ingestion (folder-watch ingester)

Build context for the RAG ingester image, referenced by the `rag` plugin
([`plugin.yaml`](plugin.yaml)) as `ordo/rag-ingestion:latest`. A small Python service that watches
a folder, chunks + embeds documents against `llamacpp-embed`, and upserts to Qdrant (`documents`
collection, 768-dim nomic space, matching the qdrant-rag MCP's query vectors). Project buildable
image, so `ordo preflight` reports a missing one as "build first".

## Build
```
docker build -t ordo/rag-ingestion:latest services/rag
```

This directory is the single source of truth for the service (`ingest.py`, `requirements.txt`,
`Dockerfile`) — the manifest references the image by name, so it can't drift from the build context.

## Hidden-path exclusion

`_is_hidden()` (`ingest.py`) excludes any file whose path has a dot-prefixed component anywhere
under `WATCH_DIR`, e.g. `.obsidian/`, `.trash/`, or `.git/` — applied to both the periodic scan
(`_iter_supported_files`) and the watchdog event path (`ingest_path`), so a hidden folder is never
indexed by either code path. There is no separate configurable exclude list: any consumer that needs
its own area of the watched tree left alone (e.g. the `evals` plugin's scratch folder, see
`services/evals/ordo_evals/checks.py`'s `VAULT_EVAL_ROOT`) gets it for free by naming that folder
with a leading dot, not by adding ingester config.

## Known limitation: deletions are not removed from Qdrant

`ingest.py` has no code path that removes a Qdrant point when its source file disappears from the
watch tree: the watchdog handler (`_EventHandler`) only implements `on_created`/`on_modified`, not
`on_deleted`, and the periodic rescan (`_iter_supported_files`) only ever adds currently-existing
files to the work queue — it never diffs against previously-ingested `state.json` entries to notice
one has gone missing. `_delete_existing` only runs inside `_upsert_points`, i.e. on a RE-ingest of
the same source path, never on absence. A file removed from the watched tree (including the whole
Obsidian vault, or the `stack-docs` mount) leaves its embedded chunks in Qdrant indefinitely, stale.

There is no ingester-side fix here yet; the eval harness (`services/evals`) works around the
consequence for its own scratch writes with an out-of-band safety check
(`ordo_evals.runner._check_rag_leak`) that fails a run if any Qdrant point's `source` is still
rooted under the eval scratch folder after cleanup, rather than relying on deletion to have worked.
