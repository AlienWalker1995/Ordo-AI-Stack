# qdrant-rag-mcp (Qdrant RAG MCP server)

Build context for the Qdrant RAG MCP image, referenced by the `qdrant-rag` plugin
([`plugin.yaml`](plugin.yaml)) as `ordo/qdrant-rag-mcp`. A small Python server that embeds
queries via `inference-engine-embed` and searches the Qdrant `documents` collection. There is no public
registry to digest-pin against, so it's a **project buildable image** (pinned by build context);
`ordo preflight` reports a missing one as "build first".

## Build
```
ordo build mcp-qdrant-rag
```

This directory (`server.py`, `requirements.txt`, `Dockerfile`) is the single source of truth for
the service.

This image runs as the long-lived compose service `mcp-qdrant-rag` (rendered into
`out/model-gateway/mcp_servers.yaml` and `out/mcp/servers.json`), serving streamable HTTP on
port 9000. Its manifest sets `network: stack` because it must reach `qdrant` and `inference-engine-embed`.

## Authority-aware search

rag-ingestion stores three fields with every chunk: `authority`, `status` and `last_verified`.
`authority` is decided by where the file lives, never by its own frontmatter:

| Authority | Where the file lives |
|---|---|
| `canonical` | The operator's source-of-truth folder, `RAG_CANONICAL_PREFIXES` (default: the vault's `notes/canonical/`) |
| `ephemeral` | the vault's `inbox/` |
| `derived` | everywhere else |

`qdrant_search`:
- leaves out chunks whose `status` is `archived` or `superseded`, and `ephemeral` chunks, unless
  `include_history` is true;
- searches only under the folders given in `folders` (a document's first four ancestor folders are stored, so a scope can be `memory-vault`, `memory-vault/notes` or deeper); with no `folders` it searches everything;
- adds `CANONICAL_BOOST` (0.05) to a canonical chunk's similarity before ranking, only when that
  chunk scores within `CANONICAL_NEAR_TIE_MARGIN` (0.03) of the best candidate, so it breaks
  near-ties and never promotes a weak match;
- returns each result's `authority` and `status` beside the raw score. Only canonical results carry
  `last_verified`; for every other result the date is the note's own unverified claim and is
  returned as `claimed_last_verified`.

Chunks indexed before these fields existed report `unknown` until rag-ingestion re-tags them: at
startup it lists every file stored under an older `META_VERSION` and rewrites its payload in place,
without re-embedding.

