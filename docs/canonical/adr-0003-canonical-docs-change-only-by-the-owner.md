---
authority: canonical
status: current
owner: operator
last_verified: 2026-11-13
verified_against: 184dd7e810b5
---

# ADR-0003: Canonical docs change only by the owner's hand; the vault is the document plane

## Status

Accepted, 2026-11-13. The code-owner file and the stricter schema test ship with this record. The owner-only rule is not enforced until cards 2 and 3 below are done: today the agent holds a credential of the owner's account.

## Context

Two kinds of documents steer the agents, and they need different rules.

**Canonical docs** (`docs/canonical/`: the PRDs, the architecture, the security model, these decision records) are the source of truth. The PR reviewer judges every change against them and search ranks them first. A source of truth is only worth that name if nothing but the owner can change it. If an agent can edit it, a tricked agent can rewrite the rules it is judged by.

**Working documents** (notes, memories, research, plans) live in the memory vault. The operator writes some, agents write others, and agents read all of them. The vault is where the operator manages documents day to day.

The state today:

| Thing | State | Where |
|---|---|---|
| Where canonical docs live | Git, this repo | `docs/canonical/` |
| Who may merge a change to them | By convention the owner. The hold is a rule in the reviewer's own skill, which the agent can edit | The reviewer's merge gate, outside this repo |
| Branch protection | One approving review and the required checks. No code-owner rule | The `main` ruleset |
| The agent's Git identity | Its own app identity, and also a token of the owner's account | `services/hermes/agent.yaml` (`secret_files`) |
| Vault writers | The agent (read-write mount), the memory-vault MCP server (read-write), the notes sync bridge (the `notes/` folder) | `services/hermes/agent.yaml`, `services/memory-vault/plugin.yaml`, `services/obsidian-livesync/plugin.yaml` |
| Vault ownership | None enforced. Agents write inside the operator's `notes/` folder | - |
| Search trust | `authority` comes from where a file lives, never from its own frontmatter | `services/rag/ingest.py` |

Three gaps follow:

1. The owner-only rule for canonical docs is enforced by software the agent can rewrite, and the agent can act as the owner on the Git host. The rule holds only while the agent chooses to keep it.
2. In the vault, the operator's own notes can be edited by any agent. A human-written note that an agent has silently changed still reads as the operator's word.
3. The schema test checks that a canonical doc declares a commit it was verified against, but never that the commit exists, and never that the links between the docs resolve.

Putting canonical docs in the vault was considered and rejected. The vault is synced, agent-readable and agent-writable by design; a folder in it can be made read-only to agents (decision 3 below), but it would still have no review, no required checks and no history the owner approves change by change. Git has all three. Mirroring the canonical docs into the vault was also considered and rejected: a second copy is a second thing that can drift or be edited, and agents already read the originals through search and the code mount.

## Decision

### 1. Canonical docs live in git, and only there

`docs/canonical/` in this repo is the one copy. It is not stored in the vault and not mirrored into it. Agents read it from the repo and through search, where its location gives it `authority: canonical`.

An install that needs canonical docs about one deployment (its hosts, its network, its other projects) keeps them in the operator's private repo under the same folder name and the same rules. They never go in this repo ([ADR-0002](adr-0002-nothing-published-identifies-the-operator.md)).

### 2. Only the repository owner changes a canonical doc

"Only the owner" is enforced by the Git host, not by the agent's instructions:

| Layer | Rule | Enforced by |
|---|---|---|
| Ownership | `docs/canonical/`, the schema test and the code-owner file itself belong to the repository owner | `.github/CODEOWNERS` |
| Merge | A PR that touches an owned path needs the owner's review. A PR the owner wrote is merged by the owner using their bypass, which is allowed on pull requests only, never on a direct push | The `main` ruleset: require review from code owners; bypass for the repository admin role, pull requests only |
| Identity | The agent holds no credential of the owner's account. It pushes, opens PRs and reviews as its own app identity, which is not an admin and not a code owner | `services/hermes/agent.yaml` |
| Courtesy | The reviewer still approves or requests changes on a canonical PR, and its merge job still skips it. This is advice to the owner, not the gate | The reviewer's skill |

The agent may propose a change to a canonical doc as a PR. It may not approve it as the owner or merge it. Code that disagrees with a canonical doc is fixed, or a PR to the doc is opened for the owner; the doc is never quietly rewritten to match the code.

### 3. The vault is the document plane, in two zones

The vault is the one place the operator manages working documents. Every top-level folder belongs to exactly one zone:

| Zone | Folders | Who writes | Who reads |
|---|---|---|---|
| Operator | `notes/` (the synced notes) | The operator, through the notes sync and the editor | The operator and every agent |
| Agent | `memories/`, `research/`, `inbox/`, `agents/` | Agents | The operator and every agent |

Rules:

1. An agent never writes in the operator zone. This is enforced by the mounts: every agent-facing container (the agent, the memory-vault MCP server, the search indexer) mounts the operator zone read-only. Only the notes sync bridge and the operator's editor write it.
2. An agent-written document lives in the agent zone, whatever it is about. Context files, episode logs and archives that agents write today under `notes/` move to the agent zone.
3. The operator may write anywhere. A document the operator wants agents to treat as theirs goes in the operator zone.
4. Search results say which zone a document came from, so an agent can tell the operator's word from another agent's.
5. No vault document outranks a canonical doc, in either zone. When they disagree, the canonical doc wins and the vault document is the one corrected.

### 4. The schema test checks what it claims

`tests/test_canonical_docs.py`, in the required Pytest job, checks for every doc under `docs/canonical/`, subfolders included:

- the frontmatter (authority, status, owner, a real past date, a successor when superseded);
- that `verified_against` is a commit in this repo's history, wherever the checkout has the history to tell;
- that every relative link in a canonical doc points at a file that exists;
- that the index links every doc.

It does not fail on age. A doc that has not been verified for a long time is reported by the memory gardener, not by a test that would break unrelated PRs on a date.

## Consequences

- A canonical change costs the owner a deliberate action every time. That is the point.
- The agent loses the owner's token. Anything that used it (pushing backups, opening PRs, commenting where the app is not installed) moves to the app identity, and the app has to be installed on every repo the agent pushes to.
- An owner-written PR to canonical docs cannot get a code-owner review from anyone else, so the owner merges it with the pull-request bypass. On a single-owner repo this is the normal path, and it is still an owner action.
- Agents that wrote under `notes/` need their paths changed, and their existing files moved once.
- The read-only mounts stop a tricked agent that uses its normal file tools. They do not stop an agent that holds a Docker socket, which can remount anything. Full enforcement of both the vault zones and rule 2's identity layer depends on [ADR-0001](adr-0001-agent-holds-no-docker-socket.md).
- Until cards 2 and 3 are done, this record describes the target, and the owner-only rule still rests on the reviewer's hold.

## Backlog

| # | Card | Done when |
|---|---|---|
| 1 | Ship the code-owner file and the stricter schema test | This record's PR is merged and the Pytest job runs the new checks |
| 2 | Add "require review from code owners" to the `main` ruleset, with a pull-request-only bypass for the repository admin role | A PR touching `docs/canonical/` that the app approved cannot be merged by the app |
| 3 | Remove the owner-account token from the agent; move its pushes, PRs and comments to the app identity | No credential of the owner's account is mounted in the agent, and its backup push and PR review still work |
| 4 | Mount the operator zone read-only in the agent, the memory-vault MCP server and the search indexer | From inside the agent, a write under `notes/` fails; the notes sync still writes it |
| 5 | Move agent-written documents out of `notes/` into the agent zone, and change the skills and crons that write them | No agent process has written under `notes/` for seven days |
| 6 | Search results carry the zone a document came from | A `qdrant_search` result for a note says `operator` or `agent` |
| 7 | The memory gardener reports canonical docs whose `last_verified` is older than 90 days | The weekly gardener output lists them, or says there are none |

Card 2 follows card 1 because the ruleset needs the code-owner file on `main`. Card 3 is what makes card 2 mean anything. Cards 4 and 5 ship together, in that order of work but one deploy, so no agent is left writing to a folder it can no longer write.
