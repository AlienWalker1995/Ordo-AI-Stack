---
name: stack-image-updates
description: The ONLY sanctioned procedure for applying image/version updates to the Ordo stack (the follow-through to the stack-audit report). Use when asked to "do the infra updates", "apply the stack audit suggestions", "update <service> to <version>", or any follow-up to the Ordo Stack Image Audit cron. Repo work only; deployment is the operator's `ordo apply`.
---

# Stack image updates

Shipped read-only in the agent image (repo: `services/hermes/skills/devops/stack-image-updates/`).
It replaces any older local copy of this skill.

Apply version/image updates suggested by the daily stack audit. You do the REPO work and open a
PR; the operator merges and deploys with `ordo apply` on the host. This split exists because two
real incidents (2026-08-06) came from skipping it.

## Hard prohibitions (each one caused a live incident, or would now)

1. **Never render.** `ordo render` from inside this container is GPU-blind and, without
   `--source out/ordo.yaml`, renders the public example over the live config. Rendering is a
   host job.
2. **Never write under `out/`.** It is generated deploy state, and it is read-only to you.
3. **Never deploy.** No compose commands, no hand-edited image tags, no recreate "to try it". You
   have no Docker access for this, and the secret store is hidden from you, so a hand-run
   compose would bring services back with blank secrets. Deployment is `ordo apply` on the host.
4. **Never merge to main.** Main is PR-protected; the operator merges.
5. **Never accept credentials pasted in chat.** If a push fails 401, stop and report which env var
   is missing (`GITHUB_PAT` / `GITHUB_PERSONAL_ACCESS_TOKEN` / `GITHUB_BACKUP_PAT`). If someone
   pastes one anyway, tell them to revoke it.

## Procedure

1. **Branch** from current main in /c/dev/ordo-ai-stack:
   `git fetch origin && git checkout -b infra-updates-$(date +%Y-%m-%d) origin/main`
2. **Edit pins in SOURCE only**: the `image:` line in `services/<id>/plugin.yaml`. Change only the
   version substring and keep every comment byte intact. First-party images (`ordo/<name>`) are
   not pinned here: they are built by `ordo build` on the host.
3. **Update the pin-lock test** (`tests/substrate/test_parity_render.py`) in the same commit, or
   the suite is red.
4. **Run the gates** from the repo root: `python -m ruff check .`, then
   `PYTHONPATH=. python -m pytest tests/substrate -q`. If anything is red that the operator has not
   documented as known: fix it or stop and report. Never push a red branch.
5. **Commit** with a conventional message listing each bump and why (security / minor).
6. **Push and verify**: `git push -u origin <branch>`, then confirm
   `git ls-remote origin refs/heads/<branch>` equals `git rev-parse HEAD`.
7. **Open a PR** with `gh pr create`: the version table, the release-note reasons, and "Deployment:
   `ordo apply` on the host after merge."
8. **Report** to the operator: versions, PR link, the test results verbatim, and the handoff line
   "After merge, on the host: `ordo apply` (it pulls, renders and recreates exactly what changed,
   lease-checked)." After the operator deploys you may verify with `list_containers` and
   `inspect_container`.

## Before you report

- [ ] Only `services/*/plugin.yaml` and the pin-lock test changed (`git status --porcelain`).
- [ ] The diff is a few lines per file, not a rewrite.
- [ ] The gate results are in the report, verbatim.
- [ ] The push is verified and the PR link is included.
- [ ] The report says what you did NOT do: no render, no deploy, no merge.
