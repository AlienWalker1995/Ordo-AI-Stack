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
SITE_LIB64=/usr/local/lib64/python3.12/site-packages

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
# Not in requirements.txt (optional GLSL shader renderer for the comfyui_underwater /
# shader nodes) — pinned here to the version this stack is validated against.
EXTRA_PINS=(comfy-angle==0.1.0)

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

# --- 4. custom node deps ----------------------------------------------------------------
# Deliberately NOT user-site: these land in the image's writable layer and are therefore
# re-resolved from scratch on every recreate. A custom node that is later removed takes its
# dependency pins with it, instead of leaving a stale copy shadowing the image forever on
# the volume. Best-effort by design — one broken node's requirements must not block boot.
install_custom_node_deps() {
  local r
  for r in "$COMFY_DIR"/custom_nodes/*/requirements.txt; do
    [ -f "$r" ] || continue
    log "installing $r"
    pip install --no-cache-dir --no-warn-script-location -q -r "$r" || log "WARN failed $r"
  done
}

reconcile_app
install_runtime_deps
seed_workflows
install_custom_node_deps

log "handing off to the image entrypoint"
exec bash /runner-scripts/entrypoint.sh
