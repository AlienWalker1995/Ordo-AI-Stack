"""stack_monitor invisible-unicode scrub (#2).

Zero-width / invisible unicode in fetched GitHub release & commit titles (e.g.
the ZWJ U+200D inside emoji sequences) must be stripped before the report is
emitted — otherwise it trips Hermes' prompt-injection scanner and blocks the
daily GitHub-monitor cron ("prompt contains invisible unicode U+200D").
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "stack_monitor.py"
_spec = importlib.util.spec_from_file_location("stack_monitor_under_test", _PATH)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)


def test_strips_zero_width_joiner():
    assert sm._strip_invisible("a‍b") == "ab"


def test_strips_common_invisibles():
    # ZWSP, ZWNJ, ZWJ, word-joiner, BOM, soft-hyphen — all Unicode category Cf.
    s = "x​‌‍⁠﻿­y"
    assert sm._strip_invisible(s) == "xy"


def test_preserves_visible_text_and_emoji():
    assert sm._strip_invisible("Release v1.2 🚀 fixes") == "Release v1.2 🚀 fixes"


def test_zwj_emoji_decomposes_to_visible_glyphs():
    # 👨‍💻 = U+1F468 U+200D U+1F4BB → stripping the ZWJ leaves the two glyphs.
    assert sm._strip_invisible("\U0001F468‍\U0001F4BB") == "\U0001F468\U0001F4BB"


def test_scrub_recurses_json_structure():
    data = {"services": {"caddy": {"name": "v2‍.11", "notes": ["a​b"]}}}
    assert sm._scrub_invisible(data) == {
        "services": {"caddy": {"name": "v2.11", "notes": ["ab"]}}
    }


def test_scrub_passes_through_non_strings():
    assert sm._scrub_invisible({"n": 3, "ok": True, "x": None}) == {"n": 3, "ok": True, "x": None}


def test_clean_output_has_no_cf_characters():
    import unicodedata
    dirty = {"a": "head‍er", "b": ["m⁠id", {"c": "tail﻿"}]}
    clean = sm._scrub_invisible(dirty)

    def _walk(o):
        if isinstance(o, str):
            return all(unicodedata.category(ch) != "Cf" for ch in o)
        if isinstance(o, dict):
            return all(_walk(v) for v in o.values())
        if isinstance(o, list):
            return all(_walk(v) for v in o)
        return True

    assert _walk(clean)
