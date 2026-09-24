"""Surgical, safe edits to the declarative source (`ordo.yaml`) — pure text → text.

The ONE `plugins:` list editor and the ONE `site:` editor. The control plane's plugin enable/disable
(`ordo/control.py`, which also backs the dashboard's MCP toggle) edits `plugins:`; the host command
`ordo remote enable/disable` (`ordo/remote.py`) edits both. Keeping them in the `ordo` package lets
the substrate tests exercise them directly (pyyaml-only, no server).

Each editor preserves every other line, comment, and the exact formatting, and REFUSES (raises
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


# A `site:` entry line at the block's indent: `  KEY: value` (the value may continue on deeper lines).
_SITE_ENTRY_RE = re.compile(r"^(?P<indent>[ \t]+)(?P<key>[A-Z][A-Z0-9_]*):(?:\s|$)")


def _site_line(indent: str, key: str, value: str, eol: str) -> str:
    """`<indent>KEY: <value>` with the value quoted exactly as YAML needs it."""
    rendered = yaml.safe_dump({key: value}, default_flow_style=False, width=10**6).rstrip("\n")
    return f"{indent}{rendered}{eol}"


def edit_site_keys(text: str, set_values: dict[str, str], remove: list[str]) -> str:
    """Set and remove keys in ordo.yaml's block-style `site:` mapping, preserving every other line.

    A key being set replaces its existing entry in place (continuation lines included) or is
    appended to the block; a removed key drops its entry. No `site:` key at all -> a block is
    appended at the end. Raises ValueError when the edit cannot be made safely (an inline
    `site: {...}` mapping, or a result that does not parse to exactly the intended mapping)."""
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    try:
        before = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"ordo.yaml does not parse: {e}") from e
    expected_site = {k: v for k, v in dict(before.get("site") or {}).items() if k not in remove}
    expected_site.update(set_values)

    key_idx = next((i for i, ln in enumerate(lines) if re.match(r"^site:", ln)), None)
    if key_idx is None:
        new_lines = list(lines)
        if new_lines and not new_lines[-1].endswith(("\n", "\r\n")):
            new_lines[-1] += eol
        if set_values:
            new_lines.append(f"site:{eol}")
            new_lines += [_site_line("  ", k, v, eol) for k, v in set_values.items()]
    else:
        if not re.match(r"^site:\s*(?:#.*)?$", lines[key_idx]):
            raise ValueError("ordo.yaml's `site:` is not a block mapping; edit it by hand")
        # Group the block into entries: (key, [line indexes]) - continuation lines join the entry above.
        entries: list[tuple[str, list[int]]] = []
        indent = "  "
        end = key_idx + 1
        while end < len(lines):
            ln = lines[end]
            match = _SITE_ENTRY_RE.match(ln)
            if match and (not entries or match.group("indent") == indent):
                indent = match.group("indent")
                entries.append((match.group("key"), [end]))
            elif ln.strip() == "" or re.match(r"^\s+#", ln):
                pass
            elif re.match(r"^\S", ln):
                break
            elif entries:
                entries[-1][1].append(end)   # a continuation line of the entry above
            else:
                raise ValueError("ordo.yaml's `site:` block has content this editor cannot read")
            end += 1
        replace = {k: idxs for k, idxs in entries}
        drop: set[int] = set()
        insert_at: dict[int, str] = {}
        for key in remove:
            drop.update(replace.get(key, []))
        pending = dict(set_values)
        for key, idxs in entries:
            if key in pending:
                drop.update(idxs)
                insert_at[idxs[0]] = _site_line(indent, key, pending.pop(key), eol)
        last_entry = max((idx for _, idxs in entries for idx in idxs), default=key_idx)
        new_lines = []
        for i, ln in enumerate(lines):
            if i in insert_at:
                new_lines.append(insert_at[i])
            elif i not in drop:
                new_lines.append(ln)
            if i == last_entry:
                new_lines += [_site_line(indent, k, v, eol) for k, v in pending.items()]

    new_text = "".join(new_lines)
    try:
        after = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"edited ordo.yaml no longer parses: {e}") from e
    if dict(after.get("site") or {}) != expected_site:
        raise ValueError("edited ordo.yaml `site:` is not the intended mapping")
    if {k: v for k, v in after.items() if k != "site"} != {k: v for k, v in before.items() if k != "site"}:
        raise ValueError("editing `site:` changed another part of ordo.yaml")
    return new_text
