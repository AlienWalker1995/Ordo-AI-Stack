## Running inside an Ordo stack: read this before "Prerequisites"

Codex is already installed here. The parts of this skill written for a single machine do not apply.

- **Never install, update or log in.** Skip `npm install` and every `codex login`. The image pins the
  CLI, and `codex` runs it on the operator's ChatGPT subscription. If `codex` exits 78 with "Codex is
  not signed in", stop and give the operator the command it prints; do not look for another
  credential and never set `OPENAI_API_KEY`.
- **`codex exec`, bounded.** Run `codex exec` with a terminal `timeout`, `pty=true`, and `--json` or
  `-o <file>` so you get the final message back. Use the interactive mode only when the operator asks.
- **Work on a branch in a scratch worktree.** For any repository under `/workspace` or `/c/dev`, create
  a git worktree under `/workspace/data/hermes/sandboxes/<task>/` on a new branch and pass it with
  `-C <worktree>`. Never let Codex commit to `main` or push unless the operator asked for exactly that.
- **Sandbox.** Use `--sandbox workspace-write`. If its sandbox cannot start in this container, use
  `--sandbox danger-full-access` only inside a scratch worktree, never in a real checkout.
- **Carry the stack's rules into the run.** Codex does not know them. Start every prompt with: "Never
  start a container that uses the GPU and never submit to ComfyUI or port 8188; renders go through the
  operator's agent. Never read, print or copy secrets, .env files, /run/secrets or ~/codex. Do not
  push or open pull requests."
- **Its report is a claim.** Read the diff (`git -C <worktree> diff`) and run the tests yourself before
  telling the operator the work is done.
- **Subscription limits.** A usage-limit or rate-limit error means wait or tell the operator; do not
  retry in a loop.
