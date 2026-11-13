# rag-ingestion (folder-watch ingester)

Build context for the RAG ingester image, referenced by the `rag` plugin
([`plugin.yaml`](plugin.yaml)) as `ordo/rag-ingestion`. A small Python service that watches
a folder, chunks + embeds documents against `inference-engine-embed`, and upserts to Qdrant (`documents`
collection, 768-dim EmbeddingGemma 2 space, matching the qdrant-rag MCP's query vectors). Project buildable
image, so `ordo preflight` reports a missing one as "build first".

## Build
```
ordo build rag-ingestion
```

This directory is the single source of truth for the service (`ingest.py`, `requirements.txt`,
`Dockerfile`) — the manifest references the image by name, so it can't drift from the build context.

## Hidden-path exclusion

`_is_hidden()` (`ingest.py`) excludes any file whose path has a dot-prefixed component anywhere
under `WATCH_DIR`, e.g. `.obsidian/`, `.trash/`, or `.git/`, applied to both the periodic scan
(`_iter_supported_files`) and the watchdog event path (`ingest_path`), so a hidden folder is never
indexed by either code path. There is no separate configurable exclude list: any consumer that needs
its own area of the watched tree left alone (e.g. the `evals` plugin's scratch folder, see
`services/evals/ordo_evals/checks.py`'s `VAULT_EVAL_ROOT`) gets it for free by naming that folder
with a leading dot, not by adding ingester config.

## Deleted, moved and no-longer-ingested files

The index follows the disk:
- a reconcile pass (`reconcile_index`) deletes every indexed source whose file is gone or that the
  rules no longer ingest (excluded prefix, hidden folder, unsupported type). It runs right after the
  first full pass at startup, then every `RECONCILE_INTERVAL_SEC` (default 600), because Docker Desktop
  bind mounts may emit no delete events;
- a delete or move event for an indexed file makes that pass run now (a move also ingests the new
  path). The event deletes nothing itself, so there is one deletion path and the guards below cover it.

Two guards stop a lost mount from wiping the index:
- a top-level folder with no files on disk (mount not ready or evicted) is skipped with a warning;
  loose files directly under the watch root never count as such a missing mount, because that
  directory always exists (it is created at startup);
- a pass that would delete more than `RECONCILE_MAX_DELETE_FRACTION` (default 0.5) of the indexed
  sources is refused and logged as an error, unless `RECONCILE_ALLOW_MASS_DELETE=1`; the guard does
  not fire below three indexed sources, where a refusal could never shed the last files.

The eval harness still keeps its own check (`ordo_evals.runner._check_rag_leak`) that no point is
rooted under the eval scratch folder after cleanup.
