"""scripts/comfyui/custom_nodes.json is the declaration boot.sh reconciles the ComfyUI node packs
to. A loose entry (branch name, short sha, duplicate name, non-GitHub url) would make the running
node code unreproducible again, so the file is held to the same bar as COMFYUI_APP_REF."""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "comfyui" / "custom_nodes.json"
BOOT = ROOT / "scripts" / "comfyui" / "boot.sh"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _packs():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["packs"]


def test_every_pack_is_pinned_to_a_full_commit_with_a_reason():
    packs = _packs()
    assert packs, "declare at least the packs the stack's graphs import"
    for p in packs:
        assert FULL_SHA.match(p["ref"]), f"{p['name']}: ref must be a full 40-hex commit, not {p['ref']!r}"
        assert NAME.match(p["name"]), f"{p['name']!r} is not a safe directory name"
        assert p["url"].startswith("https://github.com/") and p["url"].endswith(".git"), p["url"]
        assert p.get("note", "").strip(), f"{p['name']}: say why this ref"


def test_pack_names_are_unique():
    names = [p["name"] for p in _packs()]
    assert len(names) == len(set(names))


def test_name_regex_rejects_dot_and_dotdot():
    # '.' and '..' would resolve the pack dir to the ComfyUI root itself; boot.sh guards the same
    # shapes at runtime, since the manifest is parsed by python, not this file.
    assert not NAME.match(".")
    assert not NAME.match("..")
    assert NAME.match("ComfyUI-LTXVideo")
    assert NAME.match("a.b-c_9")


def test_boot_reconciles_packs_before_installing_their_requirements():
    text = BOOT.read_text(encoding="utf-8")
    body = text[text.index("\nreconcile_app\n"):]
    assert body.index("reconcile_custom_nodes") < body.index("install_custom_node_deps")
    assert "/comfyui-scripts/custom_nodes.json" in text
    # runtime guard for names the manifest regex cannot see (parsed by python, not this file)
    assert "case $name in .*|*/*)" in text


# --- reconcile_custom_nodes against a local upstream ------------------------------------
# The clone, drift and fetch-failure paths of the reconciler, exercised end to end: the function
# is lifted out of boot.sh and run with COMFY_DIR pointed at a temp dir and the manifest at a
# temp file, against a real local git repo standing in for GitHub.

def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr.strip()}"
    return r.stdout.strip()


def _commit(repo, filename, content, msg):
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _make_upstream(tmp_path, n=2):
    """A local 'upstream' repo with n commits; returns (path, [shas oldest->newest])."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q")
    _git(up, "config", "user.email", "test@example.com")
    _git(up, "config", "user.name", "Test")
    shas = [_commit(up, "node.py", f"# v{i}\n", f"v{i}") for i in range(n)]
    return up, shas


def _clone(src, dst):
    r = subprocess.run(["git", "clone", "-q", str(src), str(dst)], capture_output=True, text=True)
    assert r.returncode == 0, f"git clone failed: {r.stderr.strip()}"


def _write_manifest(path, packs):
    path.write_text(json.dumps({"packs": packs}), encoding="utf-8")


def _run_reconcile(comfy_dir, manifest_path, expect_rc=0):
    """Run boot.sh's reconcile_custom_nodes against a temp COMFY_DIR; returns CompletedProcess."""
    text = BOOT.read_text(encoding="utf-8")
    m = re.search(r"^reconcile_custom_nodes\(\) \{.*?^\}", text, re.M | re.S)
    assert m, "reconcile_custom_nodes() not found in boot.sh"
    harness = "\n".join([
        "set -euo pipefail",
        'log() { echo "[boot] $*"; }',
        'die() { echo "[boot] FATAL: $*" >&2; exit 1; }',
        f"export COMFY_DIR={shlex.quote(str(comfy_dir))}",
        f"export COMFYUI_NODE_MANIFEST={shlex.quote(str(manifest_path))}",
        m.group(0),
        "reconcile_custom_nodes",
    ])
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert r.returncode == expect_rc, (
        f"rc={r.returncode}, want {expect_rc}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
    return r


def test_missing_pack_is_cloned_to_its_ref(tmp_path):
    up, shas = _make_upstream(tmp_path)
    comfy = tmp_path / "comfy"
    (comfy / "custom_nodes").mkdir(parents=True)
    man = tmp_path / "manifest.json"
    _write_manifest(man, [{"name": "Pack", "url": str(up), "ref": shas[-1]}])
    r = _run_reconcile(comfy, man)
    pack = comfy / "custom_nodes" / "Pack"
    assert _git(pack, "rev-parse", "HEAD") == shas[-1]
    assert "node pack Pack at" in r.stdout


def test_drifted_pack_is_reset_to_its_ref(tmp_path):
    up, shas = _make_upstream(tmp_path)
    comfy = tmp_path / "comfy"
    pack = comfy / "custom_nodes" / "Pack"
    pack.parent.mkdir(parents=True)
    _clone(up, pack)
    _git(pack, "checkout", "-q", shas[0])  # drift to the older commit
    man = tmp_path / "manifest.json"
    _write_manifest(man, [{"name": "Pack", "url": str(up), "ref": shas[-1]}])
    r = _run_reconcile(comfy, man)
    assert _git(pack, "rev-parse", "HEAD") == shas[-1]
    assert "drift" in r.stdout


def test_fetch_failure_dies_and_keeps_the_old_commit(tmp_path):
    up, shas = _make_upstream(tmp_path)
    comfy = tmp_path / "comfy"
    pack = comfy / "custom_nodes" / "Pack"
    pack.parent.mkdir(parents=True)
    _clone(up, pack)
    _git(pack, "checkout", "-q", shas[0])
    man = tmp_path / "manifest.json"
    _write_manifest(man, [{"name": "Pack", "url": str(up), "ref": "0" * 40}])
    r = _run_reconcile(comfy, man, expect_rc=1)
    assert "FATAL" in r.stderr and "fetch failed" in r.stderr
    assert _git(pack, "rev-parse", "HEAD") == shas[0]  # old pack untouched


def test_drifted_pack_with_local_edits_lands_on_the_pinned_commit(tmp_path):
    up, shas = _make_upstream(tmp_path)
    comfy = tmp_path / "comfy"
    pack = comfy / "custom_nodes" / "Pack"
    pack.parent.mkdir(parents=True)
    _clone(up, pack)
    _git(pack, "checkout", "-q", shas[0])
    # A local edit to a tracked file at the old commit: the forced checkout must discard it
    # rather than refuse (which would crash-loop boot under the die posture)
    (pack / "node.py").write_text("# locally patched\n", encoding="utf-8")
    man = tmp_path / "manifest.json"
    _write_manifest(man, [{"name": "Pack", "url": str(up), "ref": shas[-1]}])
    _run_reconcile(comfy, man)
    assert _git(pack, "rev-parse", "HEAD") == shas[-1]
    assert (pack / "node.py").read_text(encoding="utf-8") == "# v1\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_checkout_failure_dies(tmp_path):
    up, shas = _make_upstream(tmp_path)
    comfy = tmp_path / "comfy"
    pack = comfy / "custom_nodes" / "Pack"
    pack.parent.mkdir(parents=True)
    _clone(up, pack)
    _git(pack, "checkout", "-q", shas[0])
    # Block the forced checkout: a read-only worktree cannot be updated (the fetch still works,
    # it writes only under .git)
    os.chmod(pack, 0o555)
    try:
        man = tmp_path / "manifest.json"
        _write_manifest(man, [{"name": "Pack", "url": str(up), "ref": shas[-1]}])
        r = _run_reconcile(comfy, man, expect_rc=1)
    finally:
        os.chmod(pack, 0o755)
    assert "FATAL" in r.stderr and "checkout failed" in r.stderr


def test_unversioned_pack_survives_a_failed_clone(tmp_path):
    up, shas = _make_upstream(tmp_path)
    comfy = tmp_path / "comfy"
    pack = comfy / "custom_nodes" / "Pack"
    pack.mkdir(parents=True)
    (pack / "local_patch.py").write_text("# hand-installed\n", encoding="utf-8")
    man = tmp_path / "manifest.json"
    _write_manifest(man, [{"name": "Pack", "url": str(up), "ref": "1" * 40}])
    _run_reconcile(comfy, man, expect_rc=1)
    assert (pack / "local_patch.py").exists(), "failed clone must not destroy the unversioned pack"
    assert not (pack / ".git").exists()
    # and the happy path replaces it with the pinned commit
    _write_manifest(man, [{"name": "Pack", "url": str(up), "ref": shas[-1]}])
    _run_reconcile(comfy, man)
    assert _git(pack, "rev-parse", "HEAD") == shas[-1]
    assert not (pack / "local_patch.py").exists()


def test_malformed_manifest_fails_loudly(tmp_path):
    comfy = tmp_path / "comfy"
    comfy.mkdir()
    man = tmp_path / "manifest.json"
    man.write_text("{ not json", encoding="utf-8")
    r = _run_reconcile(comfy, man, expect_rc=1)
    assert "unreadable" in r.stderr


def test_dotdot_name_is_rejected_before_touching_the_app_tree(tmp_path):
    up, shas = _make_upstream(tmp_path)
    comfy = tmp_path / "comfy"
    comfy.mkdir()
    (comfy / "marker.txt").write_text("app tree\n", encoding="utf-8")
    man = tmp_path / "manifest.json"
    _write_manifest(man, [{"name": "..", "url": str(up), "ref": shas[-1]}])
    r = _run_reconcile(comfy, man, expect_rc=1)
    assert "unsafe pack name" in r.stderr
    assert (comfy / "marker.txt").exists()


def test_existing_clone_is_fetched_from_the_declared_remote(tmp_path):
    # The pack was cloned from a FORK whose history lacks the declared sha: without enforcing
    # the declared url, the fetch hits the fork and the pack stays drifted forever.
    up, shas = _make_upstream(tmp_path)
    fork = tmp_path / "fork"
    fork.mkdir()
    _git(fork, "init", "-q")
    _git(fork, "config", "user.email", "test@example.com")
    _git(fork, "config", "user.name", "Test")
    _commit(fork, "node.py", "# fork-only\n", "fork commit")
    comfy = tmp_path / "comfy"
    pack = comfy / "custom_nodes" / "Pack"
    pack.parent.mkdir(parents=True)
    _clone(fork, pack)
    man = tmp_path / "manifest.json"
    _write_manifest(man, [{"name": "Pack", "url": str(up), "ref": shas[-1]}])
    _run_reconcile(comfy, man)
    assert _git(pack, "rev-parse", "HEAD") == shas[-1]
    assert _git(pack, "remote", "get-url", "origin") == str(up)


# --- the declared set: repo-owned packs + the manifest, and only they get dependencies -----------

NODES_DIR = ROOT / "services" / "comfyui" / "nodes"
PLUGIN = ROOT / "services" / "comfyui" / "plugin.yaml"


def _boot_function(name):
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", BOOT.read_text(encoding="utf-8"), re.M | re.S)
    assert m, f"{name}() not found in boot.sh"
    return m.group(0)


def _run_boot(comfy_dir, body, env=None, expect_rc=0):
    """Run boot.sh's pack functions, then `body`, against a temp COMFY_DIR, with pip stubbed to log
    its arguments to $COMFY_DIR/pip.log. Returns the CompletedProcess."""
    text = BOOT.read_text(encoding="utf-8")
    marker = re.search(r"^REPO_NODES_MARKER=.*$", text, re.M).group(0)
    harness = "\n".join([
        "set -euo pipefail",
        'log() { echo "[boot] $*"; }',
        'die() { echo "[boot] FATAL: $*" >&2; exit 1; }',
        'pip() { echo "$*" >> "$COMFY_DIR/pip.log"; }',
        f"export COMFY_DIR={shlex.quote(str(comfy_dir))}",
        *(f"export {k}={shlex.quote(str(v))}" for k, v in (env or {}).items()),
        "DECLARED_PACKS=()",
        marker,
        *(_boot_function(n) for n in ("sync_repo_nodes", "reconcile_custom_nodes", "install_custom_node_deps")),
        body,
    ])
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert r.returncode == expect_rc, (
        f"rc={r.returncode}, want {expect_rc}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
    return r


def _repo_nodes(tmp_path, packs):
    """A stand-in for services/comfyui/nodes: {pack: {relative file: content}}."""
    root = tmp_path / "repo-nodes"
    root.mkdir()
    for pack, files in packs.items():
        for rel, content in files.items():
            f = root / pack / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
    return root


def test_repo_packs_are_copied_in_and_replace_the_volume_copy(tmp_path):
    src = _repo_nodes(tmp_path, {"ordo-pack": {"__init__.py": "# repo\n", "nodes/a.py": "A = 1\n",
                                              "nodes/__pycache__/a.cpython-313.pyc": "stale"}})
    comfy = tmp_path / "comfy"
    old = comfy / "custom_nodes" / "ordo-pack"
    (old / "nodes").mkdir(parents=True)
    (old / "__init__.py").write_text("# hand-edited on the volume\n", encoding="utf-8")
    (old / "nodes" / "gone.py").write_text("# removed in git\n", encoding="utf-8")
    r = _run_boot(comfy, 'sync_repo_nodes; echo "declared: ${DECLARED_PACKS[*]}"',
                  env={"COMFYUI_REPO_NODES": src})
    assert (old / "__init__.py").read_text(encoding="utf-8") == "# repo\n"
    assert (old / "nodes" / "a.py").exists()
    assert not (old / "nodes" / "gone.py").exists(), "the volume copy must equal the repo"
    assert not (old / "nodes" / "__pycache__").exists()
    assert (old / ".ordo-repo-pack").exists()
    assert not (comfy / "custom_nodes" / "ordo-pack.tmp").exists()
    assert "declared: ordo-pack" in r.stdout


def test_a_repo_pack_removed_from_git_is_removed_but_unmarked_packs_stay(tmp_path):
    src = _repo_nodes(tmp_path, {"kept": {"__init__.py": ""}})
    comfy = tmp_path / "comfy"
    nodes = comfy / "custom_nodes"
    (nodes / "dropped").mkdir(parents=True)
    (nodes / "dropped" / ".ordo-repo-pack").write_text("", encoding="utf-8")
    (nodes / "operator-pack").mkdir()
    (nodes / "operator-pack" / "__init__.py").write_text("", encoding="utf-8")
    _run_boot(comfy, "sync_repo_nodes", env={"COMFYUI_REPO_NODES": src})
    assert (nodes / "kept").is_dir()
    assert not (nodes / "dropped").exists()
    assert (nodes / "operator-pack" / "__init__.py").exists()


def test_no_repo_nodes_mount_is_a_skip_not_a_failure(tmp_path):
    comfy = tmp_path / "comfy"
    (comfy / "custom_nodes").mkdir(parents=True)
    r = _run_boot(comfy, "sync_repo_nodes", env={"COMFYUI_REPO_NODES": tmp_path / "absent"})
    assert "skipping repo pack sync" in r.stdout


def test_dependencies_are_installed_for_declared_packs_only(tmp_path):
    up, _ = _make_upstream(tmp_path)
    ref = _commit(up, "requirements.txt", "pinned-dep==1\n", "deps")
    src = _repo_nodes(tmp_path, {"ordo-pack": {"__init__.py": "", "requirements.txt": "repo-dep==2\n"}})
    comfy = tmp_path / "comfy"
    undeclared = comfy / "custom_nodes" / "UndeclaredPack"
    undeclared.mkdir(parents=True)
    (undeclared / "requirements.txt").write_text("flash-attn\n", encoding="utf-8")
    man = tmp_path / "manifest.json"
    _write_manifest(man, [{"name": "Pinned", "url": str(up), "ref": ref}])
    _run_boot(comfy, "sync_repo_nodes; reconcile_custom_nodes; install_custom_node_deps",
              env={"COMFYUI_REPO_NODES": src, "COMFYUI_NODE_MANIFEST": man})
    installed = (comfy / "pip.log").read_text(encoding="utf-8")
    assert "custom_nodes/ordo-pack/requirements.txt" in installed
    assert "custom_nodes/Pinned/requirements.txt" in installed
    assert "UndeclaredPack" not in installed


def test_a_failed_dependency_install_does_not_block_boot(tmp_path):
    src = _repo_nodes(tmp_path, {"a": {"requirements.txt": "x\n"}, "b": {"requirements.txt": "y\n"}})
    comfy = tmp_path / "comfy"
    (comfy / "custom_nodes").mkdir(parents=True)
    r = _run_boot(comfy, "pip() { return 1; }; sync_repo_nodes; install_custom_node_deps; echo reached",
                  env={"COMFYUI_REPO_NODES": src})
    assert r.stdout.count("WARN failed") == 2 and "reached" in r.stdout


def test_submodules_of_a_pinned_pack_are_checked_out(tmp_path):
    # UltimateSDUpscale vendors its upscaler as a submodule; a clone without it imports nothing.
    sub_root = tmp_path / "sub-root"
    sub_root.mkdir()
    sub, _ = _make_upstream(sub_root)
    up, _ = _make_upstream(tmp_path)
    _git(up, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendored")
    _git(up, "commit", "-q", "-m", "add submodule")
    ref = _git(up, "rev-parse", "HEAD")
    comfy = tmp_path / "comfy"
    (comfy / "custom_nodes").mkdir(parents=True)
    man = tmp_path / "manifest.json"
    _write_manifest(man, [{"name": "Pack", "url": str(up), "ref": ref}])
    # Real packs use https submodule urls; local file:// ones need git's opt-in.
    env = {"COMFYUI_NODE_MANIFEST": man, "GIT_CONFIG_COUNT": 1,
           "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}
    _run_boot(comfy, "reconcile_custom_nodes", env=env)
    assert (comfy / "custom_nodes" / "Pack" / "vendored" / "node.py").read_text(encoding="utf-8") == "# v1\n"


def test_repo_packs_are_loadable_and_never_shadow_a_pinned_pack():
    packs = sorted(p for p in NODES_DIR.iterdir() if p.is_dir())
    assert packs, "services/comfyui/nodes holds the Ordo-authored packs"
    for pack in packs:
        assert NAME.match(pack.name), f"{pack.name!r} is not a safe directory name"
        assert (pack / "__init__.py").is_file(), f"{pack.name}: ComfyUI only loads a pack with an __init__.py"
    assert not {p.name for p in packs} & {p["name"] for p in _packs()}, "a pack is either repo-owned or pinned"


def test_boot_order_and_declared_only_installs():
    text = BOOT.read_text(encoding="utf-8")
    body = text[text.index("\nreconcile_app\n"):]
    assert body.index("sync_repo_nodes") < body.index("install_custom_node_deps")
    assert body.index("reconcile_custom_nodes") < body.index("install_custom_node_deps")
    for fn in ("install_custom_node_deps", "install_comfy_env_workers"):
        src = _boot_function(fn)
        assert '"${DECLARED_PACKS[@]}"' in src, f"{fn} must iterate the declared packs"
        assert "custom_nodes/*/" not in src, f"{fn} must not glob every pack on the volume"


def test_site_lib64_is_derived_from_the_running_interpreter():
    # A hard-coded python3.12 path outlived the move to python 3.13 and silently disabled the
    # comfy_kitchen cleanup.
    line = re.search(r"^SITE_LIB64=.*$", BOOT.read_text(encoding="utf-8"), re.M).group(0)
    assert "sysconfig" in line and "platlib" in line
    assert not re.search(r"python3\.\d+", line)


def test_comfyui_manager_is_the_pinned_pip_package():
    # --enable-manager loads the pip package; with it installed, the legacy custom_nodes pack is
    # blocked by policy, so the package is what must be pinned.
    pins = re.search(r"^EXTRA_PINS=\((.*)\)$", BOOT.read_text(encoding="utf-8"), re.M).group(1).split()
    manager = [p for p in pins if p.startswith("comfyui-manager==")]
    assert len(manager) == 1 and re.fullmatch(r"comfyui-manager==\d+(\.\d+)+", manager[0])
    assert "--enable-manager" in PLUGIN.read_text(encoding="utf-8")


def test_plugin_mounts_the_repo_nodes_read_only_as_config():
    text = PLUGIN.read_text(encoding="utf-8")
    assert "${BASE_PATH:?BASE_PATH must be set (non-empty)}/services/comfyui/nodes:/comfyui-nodes:ro" in text
    assert re.search(r"^\s*config_mounts: \[.*/comfyui-nodes.*\]$", text, re.M)
