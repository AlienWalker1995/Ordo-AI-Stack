# syntax=docker/dockerfile:1.6
# Hermes Agent for the Ordo stack: upstream's published image plus this stack's pieces.
#
# Upstream owns Hermes itself: Python, the lockfile-sealed venv, the package-manager runtime,
# SQLite, the Discord/DAVE stack, ffmpeg, git, the Docker CLI, the s6-overlay boot (UID remap,
# brain-volume ownership, config seeding) and the install stamp. Nothing here modifies it. This
# layer only ADDS: one system library, a boot hook, a plugin, a skill, a seed SOUL.md and
# helper scripts. A Hermes behavior that configuration cannot express is an
# upstream issue, not a local patch.
#
# Upstream pin: nousresearch/hermes-agent:stable at v0.21.7 (2026-10-11), by digest. Docker Hub
# carries no per-release tag, so the digest IS the pin. HERMES_PINNED_SHA names the upstream commit
# that digest was built from, and the build asserts it against the image's own provenance
# (/etc/hermes/image-provenance.json), so the two cannot disagree. scripts/stack_monitor.py reads
# HERMES_PINNED_SHA + HERMES_REPO (its `<X>_PINNED_SHA` convention) to report releases behind.
# To bump: resolve the new :stable digest (`docker buildx imagetools inspect
# nousresearch/hermes-agent:stable`), read `revision` from that file in the pulled image, and
# replace both values together.
ARG HERMES_PINNED_SHA=71d34bad1045610252eda02ee79549436896e784
ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent.git
FROM nousresearch/hermes-agent:stable@sha256:444b42594e7f467425872fc35e077a227d377f0fe0726bc818706d62175fad8b
ARG HERMES_PINNED_SHA

USER root

RUN python3 -c 'import json, sys; revision = json.load(open("/etc/hermes/image-provenance.json"))["revision"]; sys.exit(None if revision == sys.argv[1] else f"base image is upstream commit {revision}, HERMES_PINNED_SHA says {sys.argv[1]}")' "$HERMES_PINNED_SHA"

# libopus0: Discord voice. Upstream's voice docs list it as a system dependency of the gateway
# host (discord.py loads libopus.so.0 to encode and decode voice frames); the image ships only
# discord.py's bundled Windows DLLs, so without it voice playback is disabled.
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*

# git authenticates to github.com through this helper, which reads GITHUB_BACKUP_PAT from its file
# secret when asked (services/hermes/git-credential-github-pat). Nothing secret is in the config.
COPY git-credential-github-pat /usr/local/bin/git-credential-github-pat
RUN chmod 0755 /usr/local/bin/git-credential-github-pat \
    && git config --system credential.https://github.com.helper /usr/local/bin/git-credential-github-pat

# The ops-router plugin: ops-controller verbs (list/logs/inspect/restart containers, recreate a
# service) as first-class tools, the audited control-plane path for Ordo services. A bundled-source
# plugin directory, so its plugin.yaml (requires_env, provides_tools/hooks) is honored. The
# canonical ops_client.py is co-located so the plugin can `from .ops_client import OpsClient`.
COPY plugins/ops-router/ /opt/hermes/plugins/ops-router/
COPY ops_client.py /opt/hermes/plugins/ops-router/ops_client.py

# Repo-owned skill: upstream's optional ComfyUI skill plus this stack's deployment notes
# (skill-overlays/comfyui/ordo-deployment.md), spliced into SKILL.md ahead of the upstream quick
# start, which otherwise tells the agent to install and launch its own ComfyUI. The notes tell it
# to pass --host "$COMFYUI_URL" (the GPU admission gate) to every script; upstream's code is not
# modified, and its default (127.0.0.1:8188) has nothing listening in this container, so a missed
# --host fails to connect instead of skipping the lease. The result goes to /opt/ordo-skills,
# read-only, which the boot hook registers as a Hermes `skills.external_dirs` entry: the curator
# will not rewrite it and a volume loss cannot take it.
COPY skill-overlays/ /tmp/skill-overlays/
COPY scripts/ordo_skills.py /opt/ordo/ordo_skills.py
RUN python3 /opt/ordo/ordo_skills.py build \
        /opt/hermes/optional-skills/creative/comfyui \
        /tmp/skill-overlays/comfyui/ordo-deployment.md \
        /opt/ordo-skills/creative/comfyui \
    && rm -rf /tmp/skill-overlays

# The seed SOUL.md (execute, don't propose), copied into a home that has none on first boot.
COPY seed/SOUL.md /opt/ordo-seed/SOUL.md

# Vault federation: two-way sync of SOUL/memories with the Obsidian vault
# (introduced in #181; behavior in scripts/vault_federate.py's docstring).
COPY scripts/vault_federate.py /opt/ordo/vault_federate.py

# The stack's boot hook. s6-overlay runs /etc/cont-init.d/* in name order, as root, before the
# CMD: upstream's 01-hermes-setup (UID remap, ownership, seeding) first, then this one, which
# applies the stack's configuration. It is the only startup code this repo owns: there is no
# replacement entrypoint, and upstream's PID-1 init (s6) stays in charge of supervision and reaping.
# S6_BEHAVIOUR_IF_STAGE2_FAILS=2: a failing boot hook stops the container (s6-overlay's default is
# to log and start the CMD anyway, which would run a gateway with no model or secrets configured).
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2
COPY secret-env.sh /opt/ordo/secret-env.sh
COPY ordo-init.sh /etc/cont-init.d/50-ordo
RUN chmod -R a+rX,go-w /opt/ordo /opt/ordo-skills /opt/ordo-seed /opt/hermes/plugins/ops-router \
    && chmod 0755 /etc/cont-init.d/50-ordo
