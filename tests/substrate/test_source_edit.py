"""ordo/source_edit.edit_plugins_list — the shared, safe `plugins:` list editor (canonical copy,
mirrored by the dashboard's `_edit_plugins_list`). Pure text->text; refuses anything it can't guarantee."""
import pytest
import yaml

from ordo.source_edit import edit_plugins_list

SAMPLE = (
    "hardware: auto\n"
    "model: auto\n"
    "plugins:\n"
    "  - comfyui\n"
    "  - rag           # trailing comment preserved\n"
    "  - searxng-web\n"
    "cloud_fallback:\n"
    "  enabled: false\n"
)


def test_remove_drops_the_item_only():
    out = edit_plugins_list(SAMPLE, "rag", "remove")
    assert "  - rag" not in out
    assert "  - comfyui\n" in out and "  - searxng-web\n" in out   # neighbors untouched
    assert "cloud_fallback:" in out                                # rest of the doc preserved


def test_add_then_remove_round_trips():
    added = edit_plugins_list(SAMPLE, "voice", "add")
    assert "  - voice\n" in added
    assert "voice" in yaml.safe_load(added)["plugins"]
    assert edit_plugins_list(added, "voice", "remove") == SAMPLE


def test_add_is_idempotent_when_present():
    assert edit_plugins_list(SAMPLE, "comfyui", "add") == SAMPLE


def test_remove_absent_is_noop():
    assert edit_plugins_list(SAMPLE, "not-here", "remove") == SAMPLE


def test_inline_flow_list_is_refused():
    with pytest.raises(ValueError):
        edit_plugins_list("plugins: [comfyui, rag]\nmodel: auto\n", "voice", "add")


def test_missing_plugins_key_is_refused():
    with pytest.raises(ValueError):
        edit_plugins_list("model: auto\ncloud_fallback:\n  enabled: false\n", "x", "add")


def test_crlf_is_preserved():
    crlf = SAMPLE.replace("\n", "\r\n")
    added = edit_plugins_list(crlf, "voice", "add")
    assert "  - voice\r\n" in added
    assert edit_plugins_list(added, "voice", "remove") == crlf


def test_bad_action_raises():
    with pytest.raises(ValueError):
        edit_plugins_list(SAMPLE, "x", "toggle")
