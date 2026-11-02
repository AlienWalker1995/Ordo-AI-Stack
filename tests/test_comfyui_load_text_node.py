"""OrdoLoadTextFromFileNode lists the .txt files in ComfyUI's input/output/temp dirs on every
/object_info. The output dir is a 9p bind holding thousands of renders, where one stat per entry
cost ~22 s, so the listing must filter on the name and never stat a non-.txt entry."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE_FILE = ROOT / "services" / "comfyui" / "nodes" / "ordo-comfyui-nodes" / "nodes" / "load_text_node.py"


@pytest.fixture
def node_module(tmp_path, monkeypatch):
    """The node module with a stand-in `folder_paths` (ComfyUI's) pointing every dir at tmp_path."""
    dirs = {name: tmp_path / name for name in ("input", "output", "temp")}
    for directory in dirs.values():
        directory.mkdir()
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.get_input_directory = lambda: str(dirs["input"])
    folder_paths.get_output_directory = lambda: str(dirs["output"])
    folder_paths.get_temp_directory = lambda: str(dirs["temp"])
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)
    spec = importlib.util.spec_from_file_location("ordo_load_text_node_under_test", NODE_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, dirs


def test_lists_only_txt_files_case_insensitively(node_module):
    module, dirs = node_module
    (dirs["output"] / "a.txt").write_text("a", encoding="utf-8")
    (dirs["output"] / "B.TXT").write_text("b", encoding="utf-8")
    (dirs["output"] / "render.mp4").write_bytes(b"\0")
    (dirs["output"] / "folder.txt").mkdir()  # a directory with a .txt name is not a file
    assert sorted(module.OrdoLoadTextFromFileNode.get_files_for_directory("output")) == ["B.TXT", "a.txt"]


def test_input_types_prefix_each_file_with_its_dir(node_module):
    module, dirs = node_module
    (dirs["input"] / "script.txt").write_text("x", encoding="utf-8")
    (dirs["temp"] / "scratch.txt").write_text("y", encoding="utf-8")
    choices = module.OrdoLoadTextFromFileNode.INPUT_TYPES()["required"]["file"][0]
    assert choices == ["input/script.txt", "temp/scratch.txt"]


def test_empty_dirs_give_the_placeholder_choice(node_module):
    module, _ = node_module
    choices = module.OrdoLoadTextFromFileNode.INPUT_TYPES()["required"]["file"][0]
    assert choices == ["No text files found in any directory"]


def test_listing_never_stats_entries_by_path(node_module, monkeypatch):
    # The regression: os.path.isfile (a stat syscall) on every entry of a 9p dir. The listing must
    # go through os.scandir's entries, so the path-based stat helpers are never needed.
    module, dirs = node_module
    for i in range(50):
        (dirs["output"] / f"render_{i}.png").write_bytes(b"\0")
    (dirs["output"] / "keep.txt").write_text("k", encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("listing must not stat entries by path")

    monkeypatch.setattr(os.path, "isfile", forbidden)
    monkeypatch.setattr(os.path, "isdir", forbidden)
    monkeypatch.setattr(os, "stat", forbidden)
    assert module.OrdoLoadTextFromFileNode.get_files_for_directory("output") == ["keep.txt"]


def test_unreadable_dir_is_logged_not_raised(node_module, tmp_path):
    module, dirs = node_module
    dirs["output"].rmdir()
    assert module.OrdoLoadTextFromFileNode.get_files_for_directory("output") == []
