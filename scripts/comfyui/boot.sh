#!/usr/bin/env bash
# ComfyUI boot reconciler — bind-mounted read-only at /comfyui-scripts and run as the
# container command (same pattern as scripts/llamacpp/run-llama-server.sh).
#
# WHY THIS EXISTS
# ComfyUI's application tree lives on the `comfyui-app` named volume, NOT in the pinned
# runtime image (services/comfyui/plugin.yaml explains why: v0.30 startup I/O wedged the
# old 9p bind). That means the image digest pins the *runtime* — torch, CUDA, the boot
# entrypoint — but says nothing about which ComfyUI the stack is actually running. Until
# this script existed, the answer was "whatever someone last checked out by hand on the
# volume": an unpinned, undeclared, unreproducible version, i.e. exactly the config drift
# the render substrate exists to make impossible.
#
# So the version is declared (COMFYUI_APP_REF, pinned in services/comfyui/plugin.yaml) and
# reconciled here on every start. Declared == running is enforced, not hoped for.
#
# The pip pins are NOT restated here: they are read out of the checked-out ref's own
# requirements.txt. One source of truth — bumping COMFYUI_APP_REF automatically carries the
# runtime deps that version was released against, so the two can never drift apart.
set -euo pipefail

COMFY_DIR=/root/ComfyUI
# The image's platform site-packages (/usr/local/lib64/python3.X/site-packages), asked of the
# interpreter rather than written down: a hard-coded python3.12 here outlived the move to the
# python 3.13 runtime and silently turned the comfy_kitchen cleanup below into a no-op.
SITE_LIB64="$(python3 -c 'import sysconfig; print(sysconfig.get_path("platlib"))')"
# Every node pack the stack declares: the repo-owned packs (sync_repo_nodes) and the pinned
# upstream packs (reconcile_custom_nodes) append their names as they reconcile them. Steps 4 and 5
# install dependencies for these packs only, never for whatever else sits on the volume.
DECLARED_PACKS=()

log() { echo "[boot] $*"; }
die() { echo "[boot] FATAL: $*" >&2; exit 1; }

# --- 1. reconcile the application tree to the declared ref ----------------------------
# Only touches the network when the checkout has actually drifted, so the steady-state
# boot is offline-safe. When it HAS drifted and cannot be fixed, we fail hard rather than
# start a version nobody declared — a crash-looping container is a visible problem; a
# silently-wrong ComfyUI is not.
reconcile_app() {
  local ref="${COMFYUI_APP_REF:-}"
  [ -n "$ref" ] || { log "COMFYUI_APP_REF unset — skipping app reconcile"; return 0; }
  [ -d "$COMFY_DIR/.git" ] || die "$COMFY_DIR is not a git checkout; cannot reconcile to $ref"

  local head
  head="$(git -C "$COMFY_DIR" rev-parse HEAD)"
  if [ "$head" = "$ref" ]; then
    log "ComfyUI already at declared ref ${ref:0:12} ($(cat "$COMFY_DIR/comfyui_version.py" 2>/dev/null | sed -n 's/^__version__ = "\(.*\)"/\1/p'))"
    return 0
  fi

  log "ComfyUI drift: running ${head:0:12}, declared ${ref:0:12} — reconciling"
  git -C "$COMFY_DIR" fetch --tags --quiet origin \
    || die "git fetch failed; refusing to run undeclared ComfyUI ${head:0:12}"
  # Detached checkout: custom_nodes/, user/, scripts/ and the models/output mounts are
  # untracked or unchanged between refs, so they are carried across untouched.
  git -C "$COMFY_DIR" checkout --quiet --detach "$ref" \
    || die "git checkout $ref failed; refusing to run undeclared ComfyUI ${head:0:12}"
  # Stale bytecode from the previous ref shadows renamed modules (e.g. the new
  # comfy/memory_management.py split out in v0.33).
  find "$COMFY_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  log "ComfyUI now at $(git -C "$COMFY_DIR" describe --tags --always) (${ref:0:12})"
}

# --- 2. runtime deps the pinned boot image predates ------------------------------------
# The Comfy-Org runtime packages are versioned in lockstep with the app: the frontend
# speaks the app's API, and comfy-kitchen/comfy-aimdo are imported directly by
# comfy/model_management.py. Install exactly what the checked-out ref declares.
#
# Installs go to the USER site (/root/.local, on the comfyui-app volume), which precedes
# every system site-packages dir on sys.path. That is deliberate: the image ships its own
# copies in /usr/local/lib AND /usr/local/lib64, and a system-site install can land in the
# loser of that pair — the shadowing bug that silently broke every fp8/nvfp4 model load on
# 2026-08-07 ("'NoneType' object has no attribute 'Params'"). Owning the highest-priority
# path ends that class of failure instead of playing whack-a-mole with rm -rf.
COMFY_ORG_PKGS=(
  comfyui-frontend-package
  comfyui-workflow-templates
  comfyui-embedded-docs
  comfy-kitchen
  comfy-aimdo
)
# Not in requirements.txt, so pinned here to the version this stack is validated against:
# - comfy-angle: optional GLSL shader renderer for the comfyui_underwater / shader nodes.
# - comfyui-manager: the Manager that `--enable-manager` (COMFYUI_CLI_ARGS) loads. For current
#   ComfyUI it is a pip package (README "ComfyUI-Manager": `pip install -r manager_requirements.txt`,
#   then `--enable-manager`), not a custom_nodes/ComfyUI-Manager clone; with the package present,
#   comfyui_manager.should_be_disabled() blocks any legacy *comfyui-manager* pack dir by policy.
#   Without it, main.py prints "the `comfyui-manager` package must be installed first" and turns
#   the flag off. 4.3 (PyPI 2026-09-18, tag 4.3 of Comfy-Org/ComfyUI-Manager) is the latest
#   release: three security fixes over the 4.2.2 that v0.41.1's manager_requirements.txt names, and
#   no change to the ComfyUI modules it imports. warn_manager_pin_vs_ref logs a ref that moves on.
EXTRA_PINS=(comfy-angle==0.1.0 comfyui-manager==4.3)

installed_version() {
  python3 - "$1" <<'PY' 2>/dev/null || true
import importlib.metadata as m, sys
try:
    print(m.version(sys.argv[1]))
except Exception:
    pass
PY
}

install_runtime_deps() {
  local req="$COMFY_DIR/requirements.txt"
  [ -f "$req" ] || die "missing $req"

  # The image bundles comfy-kitchen in lib64. If the user-site install below ever fails we
  # must NOT silently fall back to it: the bundled copy is older than the app expects and
  # fails open (import succeeds, quant ops are None). Remove it so a broken install is loud.
  rm -rf "$SITE_LIB64"/comfy_kitchen* 2>/dev/null || true

  local specs=() pkg spec want have
  for pkg in "${COMFY_ORG_PKGS[@]}"; do
    spec="$(grep -m1 -E "^${pkg}==" "$req" || true)"
    if [ -z "$spec" ]; then
      log "WARN ${pkg} not pinned in requirements.txt at this ref — skipping"
      continue
    fi
    want="${spec##*==}"
    have="$(installed_version "$pkg")"
    if [ "$have" = "$want" ]; then
      log "dep ok: ${pkg}==${want}"
    else
      log "dep needs install: ${pkg} ${have:-<missing>} -> ${want}"
      specs+=("$spec")
    fi
  done
  for spec in "${EXTRA_PINS[@]}"; do
    pkg="${spec%%==*}"; want="${spec##*==}"
    have="$(installed_version "$pkg")"
    if [ "$have" = "$want" ]; then log "dep ok: ${spec}"; else specs+=("$spec"); fi
  done
  warn_manager_pin_vs_ref

  if [ ${#specs[@]} -eq 0 ]; then
    log "runtime deps already reconciled"
    return 0
  fi
  log "installing: ${specs[*]}"
  PIP_USER=true PIP_ROOT_USER_ACTION=ignore \
    pip install --no-cache-dir --no-warn-script-location -q "${specs[@]}" \
    || die "pinned runtime dep install failed: ${specs[*]}"

  # Verify rather than trust: pip can resolve to a different version than asked for when a
  # transitive constraint intervenes, and this is precisely the check that would have
  # caught the shadowed-kitchen outage.
  for spec in "${specs[@]}"; do
    pkg="${spec%%==*}"; want="${spec##*==}"; have="$(installed_version "$pkg")"
    [ "$have" = "$want" ] || die "after install, ${pkg} resolves to ${have:-<missing>}, expected ${want}"
  done
  log "runtime deps reconciled"
}

# The ref's own manager_requirements.txt names the Manager version that ComfyUI release was cut
# against. The pin above is deliberate (a newer release), so a difference is not an error, but it
# is logged, so a COMFYUI_APP_REF bump that moves past the pin is noticed rather than downgraded.
warn_manager_pin_vs_ref() {
  local req="$COMFY_DIR/manager_requirements.txt" spec pinned="" ref_spec
  for spec in "${EXTRA_PINS[@]}"; do
    case $spec in comfyui-manager==*) pinned="${spec##*==}";; esac
  done
  [ -n "$pinned" ] && [ -f "$req" ] || return 0
  ref_spec="$(grep -m1 -iE '^comfyui[-_]manager==' "$req" || true)"
  if [ -n "$ref_spec" ] && [ "${ref_spec##*==}" != "$pinned" ]; then
    log "NOTE comfyui-manager pinned ${pinned}; this ComfyUI ref's manager_requirements.txt names ${ref_spec##*==}"
  fi
}

# --- 3. repo-owned workflow templates ---------------------------------------------------
# Seeded into a repo-owned namespace under the user workflows dir so operator-authored
# graphs in the sibling dirs are never overwritten. Anything in `ordo/` is generated from
# the repo and replaced on every boot — edit it in git, not in the UI.
seed_workflows() {
  local src=/comfyui-scripts/workflows dst="$COMFY_DIR/user/default/workflows/ordo"
  [ -d "$src" ] || return 0
  mkdir -p "$dst"
  cp -f "$src"/*.json "$dst"/ 2>/dev/null || true
  log "seeded repo workflows -> user/default/workflows/ordo ($(ls -1 "$dst" | wc -l) file(s))"
}

# --- 3a. repo-owned node packs ----------------------------------------------------------
# The node packs Ordo itself authors live in the repo (services/comfyui/nodes/<pack>/, bind-mounted
# read-only at /comfyui-nodes) and are copied into custom_nodes/ on every start, so the volume copy
# always equals the repo: edit them in git, never on the volume. Each copy is built beside the live
# dir and swapped in whole, and carries a marker file; a marked dir whose pack has left the repo is
# removed, so deleting a pack in git deletes it from the volume too. Unmarked dirs (the pinned
# upstream packs, operator installs) are never touched here.
#
# Failure posture: these packs are declared, so a failed copy dies (as reconcile_custom_nodes does)
# rather than start ComfyUI on node code that is not the repo's.
REPO_NODES_MARKER=.ordo-repo-pack

sync_repo_nodes() {
  local src_root="${COMFYUI_REPO_NODES:-/comfyui-nodes}"
  [ -d "$src_root" ] || { log "no repo node packs at $src_root — skipping repo pack sync"; return 0; }
  local src name dst tmp marker
  for src in "$src_root"/*/; do
    [ -d "$src" ] || continue
    name="$(basename "$src")"
    dst="$COMFY_DIR/custom_nodes/$name"
    tmp="$dst.tmp"
    rm -rf "$tmp"
    cp -R "$src" "$tmp" || die "could not copy repo node pack $name"
    # Interpreter caches never travel with the source (a checkout someone ran python in has them).
    find "$tmp" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    : > "$tmp/$REPO_NODES_MARKER"
    rm -rf "$dst"
    mv "$tmp" "$dst" || die "could not install repo node pack $name"
    DECLARED_PACKS+=("$name")
    log "repo node pack $name synced"
  done
  for marker in "$COMFY_DIR"/custom_nodes/*/"$REPO_NODES_MARKER"; do
    [ -f "$marker" ] || continue
    name="$(basename "$(dirname "$marker")")"
    if [ ! -d "$src_root/$name" ]; then
      rm -rf "${COMFY_DIR:?}/custom_nodes/$name"
      log "repo node pack $name is no longer in the repo — removed"
    fi
  done
}

# --- 3b. declared custom node packs -----------------------------------------------------
# The packs the stack depends on are declared with a commit in scripts/comfyui/custom_nodes.json
# and reconciled here, the same way the app checkout is: a pack that is missing is cloned, a pack
# at another commit is fetched and detached to the declared one. Declared == running, for the
# node code too. Packs on the volume that the file does not list are operator-installed state and
# are left alone (removing them blindly would break whatever installed them); listing a pack is
# how it becomes reproducible. Offline-safe: a pack already at its ref touches no network.
#
# Failure posture — deliberately the SAME as reconcile_app: when a DECLARED pack cannot reach its
# declared ref (unreadable manifest, unreachable remote, failed fetch or checkout), boot dies
# rather than start ComfyUI on node code nobody pinned. A crash-looping container is a visible
# problem; a silently-drifted node pack is not. (The best-effort postures live in seed_workflows
# and install_custom_node_deps, where the thing being seeded or installed is not itself declared.)
reconcile_custom_nodes() {
  local manifest="${COMFYUI_NODE_MANIFEST:-/comfyui-scripts/custom_nodes.json}"
  [ -f "$manifest" ] || { log "no custom_nodes.json — skipping node pack reconcile"; return 0; }
  # Parse into a variable FIRST: had the parser run in a process substitution (<(...)), its
  # failure would be invisible to set -e and every pin below would be silently skipped, and boot
  # would run whatever packs happen to sit on the volume.
  local lines
  lines="$(python3 - "$manifest" <<'PY'
import json, sys
for p in json.load(open(sys.argv[1], encoding="utf-8"))["packs"]:
    print(p["name"], p["url"], p["ref"], sep="\t")
PY
  )" || die "custom_nodes.json unreadable: $manifest"
  local name url ref dir work head
  while IFS=$'\t' read -r name url ref; do
    [ -n "$name" ] || continue
    # The manifest is parsed by python, not validated here — reject names that would resolve the
    # pack dir outside custom_nodes/ ('.'/'..' reach the ComfyUI root itself, '/' escapes it).
    case $name in .*|*/*) die "custom_nodes.json: unsafe pack name '$name' (leading dot or slash)";; esac
    dir="$COMFY_DIR/custom_nodes/$name"
    if [ ! -d "$dir/.git" ]; then
      # Missing or unversioned (a zip copy, a hand-installed pack): build the clone in a scratch
      # dir and swap it in only once the declared commit is checked out, so a failed fetch never
      # leaves an empty git skeleton where a working pack was.
      log "node pack $name missing or unversioned — cloning ${ref:0:12}"
      work="$dir.tmp"
      rm -rf "$work"
      mkdir -p "$work" && git -C "$work" init -q && git -C "$work" remote add origin "$url" \
        || die "could not prepare clone of $name from $url"
    else
      work="$dir"
      # Enforce the declared remote: a pack cloned from a fork (or over SSH) would otherwise
      # fetch the declared sha from the wrong remote and stay drifted forever.
      git -C "$work" remote set-url origin "$url" 2>/dev/null || git -C "$work" remote add origin "$url"
    fi
    head="$(git -C "$work" rev-parse HEAD 2>/dev/null || true)"
    if [ "$head" != "$ref" ]; then
      log "node pack $name drift: ${head:0:12} -> ${ref:0:12}"
      git -C "$work" fetch -q --depth 1 origin "$ref" \
        || die "fetch failed for $name; refusing to run undeclared node pack ${head:-<none>}"
      # Forced: a drifted pack lands exactly on the pinned commit, discarding local edits to
      # tracked files (that is what listing a pack means); untracked files (downloaded weights)
      # are left in place. Without -f, a locally-modified tracked file could refuse the checkout.
      git -C "$work" checkout -q -f --detach FETCH_HEAD \
        || die "checkout failed for $name; refusing to run undeclared node pack ${head:-<none>}"
      find "$work" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    fi
    # Some packs vendor code as a git submodule pinned by the pack's own tree (UltimateSDUpscale's
    # repositories/ultimate_sd_upscale); without it the pack imports nothing. Offline-safe: a
    # submodule already at its recorded commit fetches nothing.
    if [ -f "$work/.gitmodules" ]; then
      git -C "$work" submodule update -q --init --recursive --depth 1 \
        || die "submodule update failed for $name; refusing to run an incomplete node pack"
    fi
    if [ "$work" != "$dir" ]; then
      rm -rf "$dir"
      mv "$work" "$dir"
    fi
    DECLARED_PACKS+=("$name")
    log "node pack $name at $(git -C "$dir" rev-parse --short HEAD)"
  done <<< "$lines"
}

# --- 4. custom node deps ----------------------------------------------------------------
# Deliberately NOT user-site: these land in the image's writable layer and are therefore
# re-resolved from scratch on every recreate. A custom node that is later removed takes its
# dependency pins with it, instead of leaving a stale copy shadowing the image forever on
# the volume. Best-effort by design — one broken node's requirements must not block boot.
#
# DECLARED packs only (DECLARED_PACKS: repo-owned + custom_nodes.json). Installing every pack's
# requirements.txt into the one shared env let undeclared packs drive it: one pulled spacy/blis
# sdist builds that cannot compile on py3.13, another flash-attn plus a torch pin that would have
# downgraded the runtime, and resolver conflicts (compel vs transformers) followed. A pack on the
# volume that is not declared gets no dependencies; declare it to get them.
install_custom_node_deps() {
  local name r
  for name in "${DECLARED_PACKS[@]}"; do
    r="$COMFY_DIR/custom_nodes/$name/requirements.txt"
    [ -f "$r" ] || continue
    log "installing $r"
    pip install --no-cache-dir --no-warn-script-location -q -r "$r" || log "WARN failed $r"
  done
}

# --- 5. comfy-env isolated worker envs --------------------------------------------------
# Some packs (ComfyUI-TRELLIS2, ComfyUI-GeometryPack) run their heavy nodes in a subprocess
# worker inside a pixi env that comfy-env builds from the pack's nodes/comfy-env.toml (the
# pack's own install.py is just `comfy_env.install()`, which ComfyUI-Manager would run on
# install). requirements.txt only puts comfy-env itself in the main env, so step 4 alone never
# builds the worker env. When it is missing, comfy-env silently falls back to importing the
# nodes in the main process, where DynamicVRAM (comfy-aimdo) is on and comfy.ops creates
# weights lazily: TRELLIS2's `device` property then dies with StopIteration. The worker never
# runs main.py, so aimdo stays off there and the pack works as upstream designed it.
#
# comfy-env downloads pixi from the floating `releases/latest` URL when ~/.pixi/bin/pixi is
# missing; pre-placing a pinned, checksum-verified binary there means it never does. 0.68.1 is
# the last pixi release before comfy-env 0.3.89 (the version both packs pin) was published.
#
# `comfy-env install` hashes its inputs (comfy-env version + every nodes/comfy-env.toml) into
# /root/.ce/install.hash and skips when they are unchanged, so the steady-state boot is
# offline-safe; the first run solves and downloads the envs (several GB, minutes). One run
# covers the whole workspace, later packs hit the hash. Best-effort like step 4: a failed
# solve writes no hash (so the next boot retries) and must not block every other node.
PIXI_VERSION=0.68.1
PIXI_SHA256=01d29d4b78ab07badf57edda0b3d200bc705d5afb6da9960ebabe7010cd836e4
PIXI_BIN=/root/.pixi/bin/pixi

ensure_pinned_pixi() {
  if [ -f "$PIXI_BIN" ] && echo "$PIXI_SHA256  $PIXI_BIN" | sha256sum -c --status; then
    return 0
  fi
  log "installing pinned pixi ${PIXI_VERSION}"
  local tmp="$PIXI_BIN.tmp"
  mkdir -p "$(dirname "$PIXI_BIN")"
  curl -fsSL --retry 3 -o "$tmp" \
    "https://github.com/prefix-dev/pixi/releases/download/v${PIXI_VERSION}/pixi-x86_64-unknown-linux-musl" \
    || { rm -f "$tmp"; log "WARN pixi download failed"; return 1; }
  echo "$PIXI_SHA256  $tmp" | sha256sum -c --status \
    || { rm -f "$tmp"; log "WARN pixi ${PIXI_VERSION} checksum mismatch"; return 1; }
  chmod 755 "$tmp"
  mv -f "$tmp" "$PIXI_BIN"
}

install_comfy_env_workers() {
  local name root dir
  for name in "${DECLARED_PACKS[@]}"; do
    root="$COMFY_DIR/custom_nodes/$name/comfy-env-root.toml"
    [ -f "$root" ] || continue
    command -v comfy-env >/dev/null || { log "WARN comfy-env not installed; worker envs not built"; return 0; }
    # Without the pinned binary comfy-env would fetch pixi `latest`; skip rather than float.
    ensure_pinned_pixi || { log "WARN no pinned pixi; worker envs not built"; return 0; }
    dir="$(dirname "$root")"
    log "comfy-env install for $(basename "$dir")"
    comfy-env install --dir "$dir" || log "WARN comfy-env install failed for $dir"
  done
}

reconcile_app
install_runtime_deps
seed_workflows
sync_repo_nodes
reconcile_custom_nodes
install_custom_node_deps
install_comfy_env_workers

log "handing off to the image entrypoint"
exec bash /runner-scripts/entrypoint.sh
