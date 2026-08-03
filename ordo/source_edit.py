"""Surgical, safe edits to the declarative source (`ordo.yaml`) — pure text → text.

Extracted from the dashboard (`services/v1-parity/dashboard/app.py`) so the SAME battle-tested
`plugins:` list editor backs both the dashboard's MCP toggle and the control-plane's service
enable/disable (`ordo/control.py`). Keeping it in the `ordo` package makes it the single source of
truth and lets the substrate tests exercise it directly (pyyaml-only, no server).

The editor preserves every other line, comment, and the exact formatting, and REFUSES (raises
ValueError) any edit it cannot guarantee is safe — no block `plugins:` key, an inline/flow list, an
empty list, or a result that fails to round-trip through the YAML parser with exactly the intended
change. Callers catch that and decline to persist rather than risk the operator's hand-authored source.
"""
from __future__ import annotations

import re

import yaml

# A block-list item line: `- <plugin-id>` (optionally indented) with optional trailing comment.
# Captures the indent (may be empty) + id. Zero-indent items are what `yaml.safe_dump` emits (the
# wizard's write_source), so the editor must accept them as well as the hand-authored 2-space form.
PLUGIN_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)-\s+(?P<id>[A-Za-z0-9._-]+)\s*(?:#.*)?$")


def edit_plugins_list(text: str, plugin_id: str, action: str) -> str:
    """Surgically add/remove `  - <plugin_id>` in ordo.yaml's block-style `plugins:` list, preserving
    every other line, comment, and the exact formatting. Pure text → text (no I/O), so it's unit-
    testable and the caller controls the write.

      action='remove': drop the matching item line(s). Returns text unchanged if already absent.
      action='add':    insert `  - <plugin_id>` (same indent/EOL as the last item) after the last
                       existing item. Returns text unchanged if already present.

    Raises ValueError if a safe edit can't be GUARANTEED — no block `plugins:` key, inline/flow list,
    empty list, or the result fails to round-trip through the YAML parser with exactly the intended
    change.
    """
    if action not in ("add", "remove"):
        raise ValueError(f"unknown action {action!r}")
    lines = text.splitlines(keepends=True)
    # Locate a BARE `plugins:` block key (optional trailing comment only). An inline `plugins: [a, b]`
    # has content after the colon and is deliberately rejected — it can't be line-edited safely.
    key_idx = None
    for i, ln in enumerate(lines):
        if re.match(r"^plugins:\s*(?:#.*)?$", ln):
            key_idx = i
            break
    if key_idx is None:
        raise ValueError("ordo.yaml has no block-style `plugins:` list")
    # Collect the list items in this block; stop at the next top-level key. Blank lines and indented
    # comments are treated as still inside the block (they interleave the items).
    items: list[tuple[int, str]] = []   # (line index, plugin id)
    i = key_idx + 1
    while i < len(lines):
        ln = lines[i]
        m = PLUGIN_ITEM_RE.match(ln)
        if m:
            items.append((i, m.group("id")))
            i += 1
        elif ln.strip() == "" or re.match(r"^\s+#", ln):
            i += 1
        elif re.match(r"^\S", ln):       # next top-level key — block ends
            break
        else:                            # unexpected indented, non-item content — stop, stay safe
            break
    if not items:
        raise ValueError("`plugins:` is empty or not a block-style list")

    present = [idx for idx, pid in items if pid == plugin_id]
    if action == "remove":
        if not present:
            return text
        drop = set(present)
        new_lines = [ln for j, ln in enumerate(lines) if j not in drop]
    else:  # add
        if present:
            return text
        last_idx = items[-1][0]
        m = PLUGIN_ITEM_RE.match(lines[last_idx])
        indent = m.group("indent")
        eol = "\r\n" if lines[last_idx].endswith("\r\n") else "\n"
        new_line = f"{indent}- {plugin_id}{eol}"
        new_lines = lines[:last_idx + 1] + [new_line] + lines[last_idx + 1:]

    new_text = "".join(new_lines)
    # Safety net: the edit MUST round-trip and yield exactly the intended plugins-set change, or we
    # refuse it (raise) rather than persist a broken source.
    try:
        doc = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        raise ValueError(f"edited ordo.yaml no longer parses: {e}") from e
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    if not isinstance(plugins, list):
        raise ValueError("edited ordo.yaml `plugins` is not a list")
    if action == "add" and plugin_id not in plugins:
        raise ValueError("plugin missing from `plugins` after add")
    if action == "remove" and plugin_id in plugins:
        raise ValueError("plugin still in `plugins` after remove")
    return new_text
