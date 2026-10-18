# Ordo — the config render substrate

This repo **is** Ordo: the config render engine and the stack it produces, running in
production. One declarative source (`ordo.yaml`) renders into `./out/`, and services run from that
rendered output — edits to *derived* config never survive a re-render, so drift is structurally
impossible.

The stack runs entirely from `C:\dev\ordo-ai-stack` (`main` is the production branch) as compose
project **`ordo`** (43 services in the current render, 36 plus the seven `tailnet-names` sidecars,
verified by a fresh `ordo render` with this operator's full plugin set on 2026-09-12 — see `out/docker-compose.yml`) — containers `ordo-*`, images `ordo/*`, network `ordo-net`. The
render substrate lives at the repo root (there is no `v2/` directory — there is one Ordo).

## Install & first run

**From a fresh machine — one command installs the CLI and launches the setup wizard, in the same
terminal. Run the line for your platform:**

```bash
# macOS / Linux (bash · zsh · or Git Bash on Windows)
curl -fsSL https://raw.githubusercontent.com/AlienWalker1995/Ordo-AI-Stack/main/install.sh | sh
# …or without curl:
wget -qO- https://raw.githubusercontent.com/AlienWalker1995/Ordo-AI-Stack/main/install.sh | sh
```

```powershell
# Windows — PowerShell (not cmd; `curl | sh` needs a POSIX shell, so native Windows uses this)
irm https://raw.githubusercontent.com/AlienWalker1995/Ordo-AI-Stack/main/install.ps1 | iex
```

Either path checks prerequisites (git, Docker + `docker compose` v2, Python 3.11+; warns if there's
no NVIDIA GPU), clones the repo (`~/ordo`, or `%USERPROFILE%\ordo` on Windows — override with the
`ORDO_DIR` env var), installs `ordo` into a virtualenv, and runs **`ordo init`** — the interactive
wizard that configures the whole stack.

**The wizard (`ordo init`) is the setup path**: three questions, each with a default (press
**Enter**), no accounts:

1. **Model**: the best-fit catalog pick for the detected hardware, or choose another.
2. **Features**: chat only, chat + tools, or everything this hardware supports (default).
3. **Start now?**: render, run the host checks, and `ordo up --all`.

It writes `out/ordo.yaml` + `out/secrets.env` (chmod 600, never committed; internal secrets
generated; with `--secrets-source FILE` they go to that SOPS file and `out/secrets.env` is
materialized from it, see [secrets runbook](runbooks/secrets.md)) and prints the model and plugins it chose, also under `--yes`. With the edge off, the
chat UI and dashboard publish on `127.0.0.1:8443` / `:8444` only. Remote access (Tailscale + Google
SSO) is `ordo remote enable`, any time later ([auth runbook](runbooks/auth.md)).

Onboarding is two commands: `ordo init` (say yes to the render), then `ordo up --core` (or
`--all`). The first `ordo up` builds the stack's own images, because nothing publishes them: it
builds every first-party image the rendered compose names that Docker does not have yet, then
downloads every model file the starting services load (the chat model, plus the CPU fallback and
the embedder when those are enabled) that the `models-gguf` volume lacks, checksum-verified
(`--no-fetch` skips that). There is no manual model copy step.

**Manual / already-cloned path** — the wizard just automates this; you can drive the engine directly:

```bash
ordo init                                     # re-run the wizard in an existing checkout
# …or step through it by hand:
ordo render                                   # regenerate out/ from the source, out/ordo.yaml
ordo preflight --ref out/.env                 # read-only GO/NO-GO gate, host checks included
# bring up: every rendered profile, every env file; refuses while a GPU lease holds the card.
# Builds any missing first-party image first (--no-build skips that), then fetches missing model
# files into the models-gguf volume (--no-fetch skips that; `ordo fetch` alone re-verifies them).
ordo up --all                                 # --dry-run prints the docker compose argv instead
```

`ordo up --all` reconciles the **whole** deployment against a fresh render (every profile the
render enabled). `ordo up <svc>...` / `ordo recreate <svc>...` touch only the named services
(`--no-deps`; a caddy bring-up also names its netns members, so they are recreated with it), and
refuse to start a resident the GPU scheduler evicted for a running render.
`ordo recreate --reading KEY...` recreates every long-running service whose rendered definition
reads one of those keys (after changing secrets; `ordo secrets set` / `ordo secrets rotate` print it).

### First-party images: `ordo build`

The rendered compose is image-only, so the stack's own images (`ordo/<name>`: ops-controller,
model-gateway, gpu-gate, the dashboard, the agent, the MCP adapters) are built from the checkout by
`ordo build`, never by hand:

- Each image is tagged `ordo/<name>:<commit>`: the 12-character sha of the last commit that changed
  its build context (for ops-controller, which builds from the repo root, the `ordo/`, `catalog/`
  and `services/` paths the root `.dockerignore` lets in), or `.gitattributes`, whose line-ending
  rules decide the bytes a checkout writes into the context: a commit that changes only an `eol`
  rule retags and rebuilds every first-party image. Uncommitted changes there add `-dirty`,
  and a `-dirty` tag is always rebuilt. The image also carries the full commit in the
  `org.opencontainers.image.revision` label.
- An image whose tag already exists is not rebuilt, so `ordo build --all` is cheap to repeat.
- `ordo build <name>` takes a rendered service, or a first-party image the stack does not run yet
  (`ordo build ninfer`), so an engine build a catalog model names exists before a switch picks it.
- Each build is recorded in `out/images.json`, and `ordo/<name>:current` moves to it.
- Every render (yours, and ops-controller's on a model switch) writes the recorded tag into the
  compose `image:` field, or `current` for an image not built yet. The compose therefore names the
  exact build each service runs, and `docker inspect` on a container shows its commit.

### Deploying: `ordo apply`

Deploying a new checkout, or a change to `out/ordo.yaml`, is one command:

```bash
ordo apply --dry-run                          # the plan: builds, changed services and why, lease state
ordo apply                                    # run it
```

`ordo apply` runs the steps in the one order that works, so none of them is remembered by hand:

1. `ordo build` for the first-party images whose inputs changed (an existing clean tag is skipped).
2. Render from `out/ordo.yaml` (never the example), pinning the compose to the tags step 1 recorded.
3. Materialize `secrets.env` and the file secrets from the secret store.
4. Compute the changed set: every long-running service whose rendered compose config hash or image
   id differs from its container's. The host's compose computes the hashes and does the recreate; a
   container another compose version created is recreated, not compared (versions hash differently).
   One-shot jobs (`restart: "no"`, the evals runner) are never started (see step 9). The computation
   is `ordo/render/changed_set.py`, the same one ops-controller runs after a model switch or a
   plugin enable/disable (see "Changes made through the control plane" below).
5. Refuse, before recreating anything, when the GPU lease would be violated (an evicted resident in
   the set, or ops-controller mid-lease without saved lease state) or when docker, the lease or
   ops-controller's substrate digest cannot be read.
6. The host preflight for the services that start.
7. ops-controller first when its image, config or substrate digest changed, then a wait until it
   answers `/status` (every later step reads the lease through it).
8. The rest of the changed set, `--no-deps --force-recreate` (caddy with its netns members),
   fetching the model files they load that the models volume lacks.
9. Remove the stopped one-shot job containers (created or exited) whose config or image the render
   moved past: `docker compose rm`, never a start. `run --rm` never reuses such a container, so
   nothing is lost, and the next run creates a fresh one from the current render. A job container
   that is running (an eval in progress) is left alone and reported.
10. Stop every service the render no longer defines (a plugin removed from `ordo.yaml`) that is
    not already stopped, crash loops included: stopped, not removed, so its volumes stay. ops-controller's
    apply does the same. `--only` leaves them as they are.
11. `ordo doctor`.

Nothing changed means nothing is recreated. `--only SVC...` recreates only those of the changed
services (a changed ops-controller still goes first). `ordo build`, `ordo up` and `ordo recreate`
remain for single steps; a deploy is `ordo apply`.

### Following main: `ordo deploy`

`ordo deploy` is one pass of keeping the stack on `origin/main`: when main moved since the last
deploy, it fast-forwards the checkout, runs `ordo build --all`, then `ordo apply`. It waits instead
(and tries again on the next pass) while any of these holds:

- the checkout is not on `main` (do feature work in a worktree, not in the deploy checkout);
- the checkout has uncommitted changes to tracked files, or commits that are not on `origin/main`
  (held only until the rescue below settles them into a pull request);
- a GPU lease is held;
- a one-off job container of the project is running (an eval);
- the agent is busy: a turn in flight, or a `hermes` CLI session (`hermes -z`, `hermes chat`).

Changes made on the checkout's `main` outside a pull request (an agent's local commit, or edits to
tracked files) are never discarded and never deployed as they are. Once they have been unchanged for
30 minutes, `ordo deploy` replays them onto `origin/main` as one new commit (a three-way merge
without touching the checkout, so no local history is published), scans exactly that commit for
secrets with the TruffleHog release CI runs (any finding, verified or not, stops it and is reported
once), pushes it to a `deploy-rescue/<time>` branch and opens a pull request, which gets the usual
review. Deploys hold while it is open. Newer edits update it in place, but never over a commit
someone else pushed to it (a review fix): those edits get a pull request of their own. Merge it to
keep the changes, or close it to drop them (they stay on the pull request). Either way the next pass
resets the checkout to `origin/main` and deploys, but only when the pull request's history still
holds the rescued commit and the checkout has not changed since.

Two cases need no rescue. Changes `origin/main` already holds reset the checkout at once. A local
commit that a pull request already holds (an agent committed here and opened a PR from it) follows
that pull request: deploys hold while it is open, and the checkout resets once it is merged or
closed. Changes that conflict with `origin/main` are reported once and wait for the operator. The
host's `gh` must be signed in; until it is, the deploy waits and says why.

The gates are read again after the build, right before `ordo apply`, and an `ordo apply` that refuses
before recreating anything (a lease taken in between) is a wait too, tried again on the next pass. A deploy that waits an hour is
posted once to the Discord channel Alertmanager uses (`ALERTMANAGER_DISCORD_WEBHOOK_URL`), with the
gate holding it, and so is every deploy and every failure. A failed deploy is not retried: push a
fix or a revert to main and the next pass deploys that.

```bash
ordo deploy --install-schedule    # a scheduled task (Windows) runs a pass every 2 minutes
ordo deploy --status              # the last deploy: commit, result, its log under out/deploy/logs/
ordo deploy                       # one pass by hand
```

On Windows the task runs under `conhost --headless` (no window, for it or its git and docker
children) as the operator while they are signed in (when Docker Desktop runs), and appends each pass
that did something to `out/deploy/deploy.log`. Elsewhere
`--install-schedule` prints the crontab line to add.

### Changes made through the control plane

A model switch (the dashboard's Models page, `set_active_model`, `POST /model-config`) and a plugin
enable or disable (Hermes' `enable_service`, `POST /plugins/{id}/...`)
write `ordo.yaml`, render, and then apply the render from ops-controller: it recreates exactly the
changed set (`--no-deps --force-recreate`, netns members with their owner, refused while it would
start a GPU-evicted resident) and stops the services the render dropped. It cannot recreate itself or
the agent calling it, a container another compose version created, or a service whose secrets
`out/secrets.env` lacks: those come back as `restart_required_on_host` with the command that
finishes the job (for example `ordo apply --only agent` after a context-window change, since the
agent reads `LLAMACPP_CTX_SIZE`). A failed apply restores the previous `ordo.yaml`, re-renders and
re-applies it. Like `ordo apply`, it removes the stopped one-shot job containers the render moved
past (`removed_jobs`) and leaves a running one alone (`running_jobs`). The control plane never reads
the checkout, so an edited config file (a declared `config_mounts:` bind, `ordo/render/bind_configs.py`)
never makes it recreate a service. A service it recreates for its own reason still mounts the file as
it is now: after an edit to `scripts/llamacpp/` that `ordo apply` has not deployed, a model switch
starts llamacpp on the edited script, and the next `ordo apply` recreates llamacpp once more to
record the new digest. `POST /apply`
(`{"dry_run": true}` for the plan, else `{"confirm": true}`) applies whatever `out/` holds now. model-gateway and model-gateway-keys carry a digest of the
`out/model-gateway/` files they read at startup (`ordo.rendered-config`), so an MCP toggle changes
their config hash and is in the changed set.

Rolling back is `ordo apply` on older code. Check out the older commit and run it: its tags are
still in the local image cache, so the build step records them without rebuilding, the render points
the compose back at them, and only what changed is recreated. (A `git revert` works too; it is a new
commit, so it gets a new tag, built mostly from Docker's layer cache.) Nothing is retagged by hand.

Everything below is the reference for *how* that render engine works and *why* it's built this way.

## Why this exists (from the architecture interrogation)

Nearly every failure of the current stack traced to **config drift**: the LLM context size,
the model choice, and Hermes' `context_length` were hand-set in three places and fell out of
sync (256K in Hermes vs 128K in llama.cpp → a compaction deadlock; a stale model registry vs
`.env`; etc.). The agreed cure is a **declarative source → regenerated config** model:

> One human-editable declarative source (`ordo.yaml`). Everything derived (`.env`, Hermes
> context, model-gateway ctx, compose vars) is **regenerated** from it. Edits to *derived*
> outputs don't survive a re-render — so drift is structurally impossible. An explicit
> `overrides:` block in the source is the escape hatch that *does* survive (today it reaches the
> llama.cpp tuning keys only; any other key is a render error).

This is the substrate everything else (scheduler, plugins, installer) renders through, and it's the
direct fix for the #1 pain — now proven in production, not just in test.

## What's here

| File | Role |
|---|---|
| `ordo.example.yaml` | the declarative source — the single source of truth (hardware, tier, model, plugins, overrides) |
| `catalog/models.yaml` | curated model catalog: each entry has resource requirements **and a sha256** (checksums are mandatory — corrupt weights burned us once) |
| `ordo/render/hardware.py` | hardware detection (GPU/VRAM/RAM/CPU) + mockable profiles for CI |
| `ordo/render/catalog.py` | load catalog + **best-fit** model selection with a VRAM headroom reserve (encodes the "don't fill the card" lesson) |
| `ordo/render/config.py` | load/validate the declarative source |
| `ordo/render/engine.py` | `(source + hardware + catalog + plugins) → RenderedConfig`; writes `out/.env`, `out/manifest.json` |
| `ordo/render/plugins.py` + `services/*/plugin.yaml` | **registry-driven** plugins: each manifest declares hardware needs + a config fragment; the renderer enables what fits (media = NVIDIA-only) and resolves `depends_on` |
| `ordo/control/scheduler.py` | GPU **scheduler decision engine** — FIFO admission + co-run-when-it-fits + LRU idle-evict (replaces the reactive guardian that caused the outage; the process broker drives it against the real `ordo-` containers — live in production) |
| `ordo/cli.py` | `ordo detect | render | doctor | serve | preflight | …` — the one-script control surface |
| `tests/substrate/` | mocked-profile render (5090 + CPU-only), drift-revert, ctx consistency, plugin gating/deps, scheduler co-run/FIFO/evict, and per-defect-class regression guards from the parity audits (run below) |

## Operating this stack
The rendered services run under compose project `ordo` from `C:\dev\ordo-ai-stack`, all reached through
the edge: Caddy is the **only** service that publishes host ports, one port per UI surface on
`${CADDY_TAILNET_HOSTNAME}`. The port map (which service, which port, how each signs in) lives in [configuration.md's Network Ports](configuration.md#network-ports).

One Google sign-in covers every oauth2-proxy-gated port (all but Hermes, which signs in itself) **and the clean per-service tailnet names** — the
oauth2-proxy cookie is domain-scoped and the SSO gate's `rd=` carries `{host}` (portless), so a
single wildcard `--whitelist-domain=.<domain>` covers every port and sidecar name at once; the Google
OAuth client needs no new redirect URIs. Old subpath URLs (`/chat`, `/dash`, `/comfy`,
`/hermes`, `/codebase-memory`, `/grafana`) 302 from `:443` to their new ports, so bookmarks keep
working. This
retires the subpath-rewrite class of workaround (Open WebUI root-catchall, Hermes header-based
base injection, codebase-memory nginx rewrites) in favor of giving every
prebuilt SPA the root it was actually compiled for. One data root at
`C:\dev\ordo-ai-stack\data`; stateful services (the Hermes brain included) keep their state on
named volumes. Secrets come from one store (a SOPS file in a private repo, or Infisical), materialized
into gitignored `out\secrets.env` by `ordo secrets materialize`; see
[runbooks/secrets.md](runbooks/secrets.md).

This Tailscale front door is the **default of three swappable access layers** — the edge
(`services/edge`) and clean-URL names (`services/tailnet-names`) are plugins, so the same rendered stack
can instead sit behind a self-hosted public domain or a cloud VM (both keeping the same Google SSO
gate). Only the Tailscale model is wired today; the others' required pieces are documented in
[`deployment-models.md`](deployment-models.md).

**Render discipline** (the drift cure, in daily operation):
- Change config by editing the source `ordo.yaml`, then **re-render** — never hand-edit `out/.env`.
- `ordo render` (like every command) reads the real source, `out/ordo.yaml`, when `--source` is not given.
- Deploy source and image changes with `ordo apply` (see "Deploying" above); it builds, renders and
  recreates exactly the changed services, ops-controller first.
- Recreate a single service with `ordo recreate <svc>` (per-service, no cascade). The dashboard's
  per-service recreate button does exactly this against the existing `out/` compose (no re-render).

**Which engine answered?** While a render holds the GPU lease, the GPU chat model is evicted and
`local-chat` traffic is served by the always-on CPU fallback inside the gateway; the response
still says `local-chat`. The gateway is the one place every client goes through, so it is where the
answer lives: each request's trace in Langfuse (when the langfuse plugin is enabled) names the
deployment that served it, every gateway response carries the `x-litellm-model-name` header, the
Grafana "Ordo performance" dashboard plots throughput per deployment, the dashboard's Overview
shows which engine answers chat right now, and the `ChatServedByCpuFallback` alert fires to
Discord about two minutes after an eviction and resolves when the GPU model is back. Clients
(Hermes included) use `local-chat` and leave the routing to the gateway; `local-chat-gpu` is for
work that must run on the GPU and accepts an error while it is away.

**Backups:** `ordo backup` saves `out/ordo.yaml`, `out/secrets.env`, `out/images.json` and every stateful
volume (databases dumped, or copied with their writers stopped) to one owner-only archive outside the
checkout; `ordo restore <archive>` puts it back. See [data.md](data.md#backup-and-recovery).

## Run the tests (no host Python needed)
```
docker run --rm -v "$PWD:/w" -w /w python:3.11-slim \
  sh -c "pip install -q -r requirements-dev.txt && PYTHONPATH=. python -m pytest tests/substrate -q"
```
