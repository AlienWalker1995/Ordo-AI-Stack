---
authority: canonical
status: current
owner: operator
last_verified: 2026-11-12
verified_against: 1c97d8f3608b
---

# ADR-0002: Nothing published from this repo identifies the operator

## Status

Accepted, 2026-11-12. The checker, the pre-push hook and the pull-request workflow ship with this record; the backlog below lists what is left.

## Context

This repository is public and Ordo is a generic platform. The operator also runs it, so their own names keep arriving in it: a host name in a fixture, a real address in a test, another project's name as an example, a dated incident in a comment, container counts in a pull request body.

Publishing is wider than tracked files, and it is permanent in practice. All of these are public the moment they are pushed or posted, and forks, clones and the host's cached pull-request refs keep what a later rewrite removes:

- tracked files, including their paths;
- commit messages, and each commit's author and committer address;
- pull request and issue titles, bodies and comments;
- CI logs and uploaded artifacts;
- screenshots and other images.

The earlier guard checked tracked files only, for three patterns, in a test job that runs only when the render engine changes. It also had to spell the operator's names to search for them, which published them. It missed real addresses, other project names, an encrypted secret store that had been force-added, personal author addresses on commits, and everything written in pull requests.

## Decision

Nothing published from this repository may identify the operator, their machines, their network or their other projects, or describe one real deployment's weaknesses. One checker enforces it over everything that gets published, and the operator's own names are kept outside the repo.

### What never appears

| Never | Use instead |
|---|---|
| A person's name, a personal or work email address, a chat-platform user, server or channel id | `me@example.com`, the fixture ids the tests already use |
| A commit author address that is a real mailbox | The Git host's noreply address |
| A real tailnet name, MagicDNS name, host name, LAN or tailnet address, domain | `<tailnet>.ts.net`, `tail1234`, `100.64.0.x`, `example.com` |
| A user's home path or a path that shows one machine's layout | `<repo>`, `/home/me/dev`, "the checkout" |
| The name of the operator's private repo, or of their other compose projects, sites and services | A generic name (`media-stack`, `homepage`), `../ordo-secrets/` for the documented default |
| A secret, in any form, including an encrypted store (`*.sops`, `*.age`), a key or a `.env` | Nothing. The secret store lives in a private repo ([secrets runbook](../runbooks/secrets.md)) |
| Identifiers of external accounts: OAuth client ids, cloud project ids, tunnel ids, webhook URLs | A placeholder of the same shape |
| The state of one real deployment: which address it binds, what it exposes, what is unpatched, where it is, what it costs to run | The product's behaviour and its options, stated generally |

### What is fine

- Documenting a limitation of the product ("the agent mounts the Docker socket"), because it is true of every install and a reader needs to know it.
- The reasoning behind a design, including that a failure happened, without the detail that identifies where ("a leaseless GPU job crashed the host").
- Names of third-party software the product integrates with or monitors.
- Hardware named as an example of a class ("a 32 GB card").

The test for a sentence: would it still be true, and still useful, on someone else's install? If it is only true of the operator's, it belongs in the private repo.

### Enforcement

One checker, `scripts/ci/public_hygiene.py`, standard library only, run at three points:

| Where | When | Covers |
|---|---|---|
| `.githooks/pre-push` | Before a push leaves the machine | Tracked files, and the messages and addresses of the commits being pushed |
| `Public hygiene` workflow | Every pull request, again when its title or body is edited | The same, plus the pull request's title and body |
| `tests/test_public_repo_hygiene.py` | The required Pytest job | Tracked files, and the checker's own behaviour |

The pre-push hook is the only one that prevents a leak; the other two report one that has already been published. Enable it once per clone with `git config core.hooksPath .githooks`.

The checker has two layers:

1. **Generic shapes**, written in the checker: a concrete tailnet id, an email address outside the placeholder domains, a LAN or tailnet host address, a user's home path, a 17 to 20 digit chat id, and any tracked file shaped like a secret store.
2. **The operator's denylist**: their own names, one regular expression per line. It lives outside this repo. CI reads it from the `ORDO_HYGIENE_DENYLIST` repository secret; the hook reads the file named by `git config ordo.hygieneDenylistFile` (or `ORDO_HYGIENE_DENYLIST_FILE`).

A finding names the file, the line and the kind of thing found. It never prints the match, because the log it is printed to is public.

The Git host's own secret scanning and push protection stay on for this repository.

### Rules a PR is judged against

1. A PR that adds an identifier from the table above is rejected, whatever else it does. So is a PR whose title, body or commits carry one.
2. Evidence from the operator's live deployment (container counts, project names, host names, dated incidents on a named machine) goes in the private repo or stays out of the PR. A PR body describes the change and how it was validated in terms any install would share.
3. An exception is a change to the checker's allowlist, reviewed as a change to this record. A file is never exempted to make a failure go away.
4. A new kind of identifier found leaking gets a pattern in the checker in the same PR that removes it, so the same shape cannot return.
5. A new project, host or domain the operator starts using is added to the denylist before it is used anywhere near this repo.

## Consequences

- Fixtures, examples and docs use generic names. A test that needs a "real-looking" value uses an invented one from the placeholder set.
- A PR body can no longer carry live-deployment evidence. Validation is described in terms any install shares; the raw evidence stays private.
- Two things live outside the repo and have to be kept: the denylist, and its copy in the CI secret. A name missing from the denylist is not checked.
- The hook has to be enabled per clone. A clone without it is covered only by CI, which reports after publication.
- The generic shapes will sometimes flag an innocent value (a long number, an unfamiliar example domain). The fix is a placeholder from the allowed set, not an exemption.
- When something leaks anyway, it is treated as disclosed: rotate the secret, rename the identifier or accept the exposure. Removal is not a remedy. It is removed from the tree in a normal PR, with the pattern that would have caught it. Rewriting history is the operator's decision: it clears this repository's branches only, changes every later commit id, and invalidates every open branch and clone.

## Backlog

| # | Card | Done when |
|---|---|---|
| 1 | Ship the checker, the hook, the workflow and the cleaned tree | This record's PR is merged and the `Public hygiene` check has run green on a later PR |
| 2 | Make `Public hygiene` a required status check on `main` | A PR with a leaking title cannot be merged |
| 3 | `ordo doctor` reports a clone whose `core.hooksPath` is not `.githooks`, or that has no denylist configured | A fresh clone without the hook fails doctor with the command that fixes it |
| 4 | Give the agent's container the denylist, so its own pushes get both layers | A push from inside the agent with a denylisted name in a commit message is refused |
| 5 | Replace `docs/assets/overview-statusline.png` with a capture from demo data | The image shows no real hardware inventory, disk size or dates |
