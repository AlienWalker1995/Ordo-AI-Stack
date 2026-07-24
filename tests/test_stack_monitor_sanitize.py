"""stack_monitor invisible-unicode scrub (#2).

Zero-width / invisible unicode in fetched GitHub release & commit titles (e.g.
the ZWJ U+200D inside emoji sequences) must be stripped before the report is
emitted — otherwise it trips Hermes' prompt-injection scanner and blocks the
daily GitHub-monitor cron ("prompt contains invisible unicode U+200D").

`scrub(text)` is applied per string field (reason/note/highlight) as the audit
result is assembled — there's no separate whole-tree walker, so these tests
exercise `scrub()` directly, including in the same per-field pattern audit()
uses on a small nested structure.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "stack_monitor.py"
_spec = importlib.util.spec_from_file_location("stack_monitor_under_test", _PATH)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)


def test_strips_zero_width_joiner():
    assert sm.scrub("a‍b") == "ab"


def test_strips_common_invisibles():
    # ZWSP, ZWNJ, ZWJ, word-joiner, BOM, soft-hyphen — all Unicode category Cf.
    s = "x​‌‍⁠﻿­y"
    assert sm.scrub(s) == "xy"


def test_preserves_visible_text_and_emoji():
    assert sm.scrub("Release v1.2 🚀 fixes") == "Release v1.2 🚀 fixes"


def test_zwj_emoji_decomposes_to_visible_glyphs():
    # 👨‍💻 = U+1F468 U+200D U+1F4BB -> stripping the ZWJ leaves the two glyphs.
    assert sm.scrub("\U0001F468‍\U0001F4BB") == "\U0001F468\U0001F4BB"


def test_scrub_handles_none_and_empty():
    assert sm.scrub(None) == ""
    assert sm.scrub("") == ""


def test_scrub_applied_per_field_like_audit_does():
    # audit() scrubs each text field individually rather than walking a whole
    # JSON tree — mirror that usage on a small nested structure.
    data = {"services": {"caddy": {"reason": "v2‍.11 up to date", "note": "a​b"}}}
    cleaned = {
        "services": {
            svc: {field: sm.scrub(val) for field, val in fields.items()}
            for svc, fields in data["services"].items()
        }
    }
    assert cleaned == {"services": {"caddy": {"reason": "v2.11 up to date", "note": "ab"}}}


def test_clean_output_has_no_cf_characters():
    import unicodedata

    dirty = ["head‍er", "m⁠id", "tail﻿"]
    clean = [sm.scrub(s) for s in dirty]
    assert all(unicodedata.category(ch) != "Cf" for s in clean for ch in s)
