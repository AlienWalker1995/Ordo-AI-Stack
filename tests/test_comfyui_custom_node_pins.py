"""scripts/comfyui/custom_nodes.json is the declaration boot.sh reconciles the ComfyUI node packs
to. A loose entry (branch name, short sha, duplicate name, non-GitHub url) would make the running
node code unreproducible again, so the file is held to the same bar as COMFYUI_APP_REF."""
import json
import re
import shlex
import subprocess
from pathlib import Path

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
