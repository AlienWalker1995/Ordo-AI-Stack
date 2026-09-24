"""ordo/source_edit.edit_plugins_list: the one `plugins:` editor, used by ops-controller's plugin
enable/disable (which also backs the dashboard MCP toggle). Pure text->text; refuses anything it
can't guarantee."""
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


# --- edit_site_keys: the `site:` editor `ordo remote enable/disable` uses ---

from ordo.source_edit import edit_site_keys  # noqa: E402

SITE_SAMPLE = (
    "# my source\n"
    "model: auto\n"
    "plugins: auto\n"
    "site:\n"
    "  BASE_PATH: /srv/ordo    # host checkout\n"
    "  LONG_ARGS: --a --b\n"
    "    --c --d\n"
    "  DATA_PATH: /srv/ordo/data\n"
    "cost: {}\n"
)


def test_set_adds_and_replaces_keeping_everything_else():
    out = edit_site_keys(SITE_SAMPLE, {"CADDY_BIND": "127.0.0.1", "DATA_PATH": "/data"}, [])
    doc = yaml.safe_load(out)
    assert doc["site"] == {"BASE_PATH": "/srv/ordo", "LONG_ARGS": "--a --b --c --d",
                           "DATA_PATH": "/data", "CADDY_BIND": "127.0.0.1"}
    assert "# my source\n" in out and "# host checkout" in out and doc["cost"] == {}


def test_remove_drops_a_key_and_its_continuation_lines():
    out = edit_site_keys(SITE_SAMPLE, {}, ["LONG_ARGS", "NOT_THERE"])
    assert yaml.safe_load(out)["site"] == {"BASE_PATH": "/srv/ordo", "DATA_PATH": "/srv/ordo/data"}
    assert "--c --d" not in out


def test_a_missing_site_block_is_appended():
    out = edit_site_keys("model: auto\n", {"CADDY_BIND": "0.0.0.0"}, [])
    assert yaml.safe_load(out) == {"model": "auto", "site": {"CADDY_BIND": "0.0.0.0"}}


def test_values_are_quoted_when_yaml_needs_it():
    out = edit_site_keys(SITE_SAMPLE, {"WEIRD": "a: b # c"}, [])
    assert yaml.safe_load(out)["site"]["WEIRD"] == "a: b # c"


def test_an_inline_site_mapping_is_refused():
    with pytest.raises(ValueError):
        edit_site_keys("site: {A: b}\n", {"CADDY_BIND": "0.0.0.0"}, [])
