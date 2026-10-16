"""scripts/comfyui/custom_nodes.json is the declaration boot.sh reconciles the ComfyUI node packs
to. A loose entry (branch name, short sha, duplicate name, non-GitHub url) would make the running
node code unreproducible again, so the file is held to the same bar as COMFYUI_APP_REF."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "comfyui" / "custom_nodes.json"
BOOT = ROOT / "scripts" / "comfyui" / "boot.sh"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


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


def test_boot_reconciles_packs_before_installing_their_requirements():
    text = BOOT.read_text(encoding="utf-8")
    body = text[text.index("\nreconcile_app\n"):]
    assert body.index("reconcile_custom_nodes") < body.index("install_custom_node_deps")
    assert "/comfyui-scripts/custom_nodes.json" in text
