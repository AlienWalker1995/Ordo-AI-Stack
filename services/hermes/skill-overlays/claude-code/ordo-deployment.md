## Running inside an Ordo stack: read this before "Prerequisites"

Claude Code is already installed and signed in here. The parts of this skill written for a single
machine do not apply.

- **Never install, update or log in.** Skip `npm install`, `claude update`, `claude auth login` and
  `claude setup-token`. The image pins the CLI, and `claude` runs it on the operator's Claude
  subscription with a token from the stack's secret store. If `claude` exits 78 with "no Claude Code
  token is configured", stop and give the operator the command it prints; do not look for another
  credential and never set `ANTHROPIC_API_KEY`.
- **Only Claude, only through `claude`.** Claude Code runs Anthropic's models on the operator's
  subscription and nothing else. Never run the binary under `/opt/claude-code` directly, never set
  `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` or a model override, and never point it at the stack's
  LiteLLM gateway or the local model: the image's managed settings force the subscription, so such a
  run only fails. Local-model work is yours to do, not Claude Code's.
- **Print mode, bounded.** Use `claude -p` with `--max-turns` and a terminal `timeout`, and add
  `--output-format json` so you get the result, the session id (resume with `-r <id>`) and the cost.
  Use the tmux mode only when the operator asks for an interactive session.
- **Work on a branch in a scratch worktree.** For any repository under `/workspace` or `/c/dev`, create
  a git worktree under `/workspace/data/hermes/sandboxes/<task>/` on a new branch and point `workdir`
  there. Never let Claude Code commit to `main` or push unless the operator asked for exactly that.
- **Grant only what the task needs.** In print mode a tool that is not allowed is refused, so pass
  `--allowedTools` scoped to the task (for example `"Read,Edit,Write,Bash(git diff *),Bash(pytest *)"`)
  and `--permission-mode acceptEdits` for edits. `--dangerously-skip-permissions` only inside a scratch
  worktree, never in a real checkout.
- **Carry the stack's rules into the run.** Claude Code does not know them. Pass
  `--append-system-prompt "Never start a container that uses the GPU and never submit to ComfyUI or
  port 8188; renders go through the operator's agent. Never read, print or copy secrets, .env files or
  /run/secrets. Do not push or open pull requests."` on every call.
- **Its report is a claim.** Read the diff (`git -C <worktree> diff`) and run the tests yourself before
  telling the operator the work is done.
- **Subscription limits.** A usage-limit or rate-limit error means wait or tell the operator; do not
  retry in a loop.
