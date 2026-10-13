"""html-video: the template library and the HTTP contract, without a browser.

The service's one hard dependency is headless Chromium, which CI does not run. Everything
else is pure and tested here: the card data validation (an agent writes the data, so every
problem must be named), the Jinja rendering (autoescaped: data can never inject markup), the
request contract (`build_spec`) and the routes against a fake renderer. The real capture loop is
exercised by the pipeline's end-to-end render, not by CI.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "html-video"


def _load(name: str, filename: str):
    # app.py imports its sibling cards.py as it does from /app in the image.
    if str(SERVICE_DIR) not in sys.path:
        sys.path.insert(0, str(SERVICE_DIR))
    spec = importlib.util.spec_from_file_location(name, SERVICE_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cards = _load("cards", "cards.py")
kit = _load("kit", "kit.py")
app_module = _load("html_video_app", "app.py")


# --- the template library ---------------------------------------------------------------------

@pytest.mark.parametrize("template_id", sorted(cards.TEMPLATES))
def test_every_template_example_validates_and_renders(template_id):
    template = cards.TEMPLATES[template_id]
    html, rendered_template = cards.render_html(template_id, template.example)
    assert rendered_template is template
    assert html.startswith("<!doctype html>")
    assert '/assets/cards.css' in html
    assert (SERVICE_DIR / "templates" / template.file).is_file()
    assert template.duration > 0


@pytest.mark.parametrize("template_id", sorted(cards.TEMPLATES))
def test_every_template_is_described(template_id):
    described = cards.describe()[template_id]
    assert described["duration"] == cards.TEMPLATES[template_id].duration
    assert described["example"] == cards.TEMPLATES[template_id].example
    assert set(described["fields"]) == {f.name for f in cards.TEMPLATES[template_id].fields}
    json.dumps(described)  # the catalogue is what GET /templates returns


def test_data_is_escaped_never_injected():
    html, _ = cards.render_html("stat", {"value": "<b>1</b>", "label": "x & y"})
    assert "<b>1</b>" not in html
    assert "&lt;b&gt;1&lt;/b&gt;" in html
    assert "x &amp; y" in html


def test_validation_names_every_problem_at_once():
    with pytest.raises(cards.CardDataError) as err:
        cards.validate("stat", {"value": "x" * 11, "tone": "loud", "bogus": 1})
    message = str(err.value)
    assert "value: 11 characters, max 10" in message
    assert "label: required" in message
    assert "tone: must be one of neutral, hype, bust" in message
    assert "bogus: unknown field" in message


def test_optional_choice_defaults_to_first_choice():
    out = cards.validate("stat", {"value": "1", "label": "one"})
    assert out["tone"] == "neutral"
    assert out["note"] is None
    out = cards.validate("list", {"title": "t", "items": [{"text": "a"}, {"text": "b"}]})
    assert [item["mark"] for item in out["items"]] == ["check", "check"]


def test_list_sizes_and_nested_objects_are_checked():
    with pytest.raises(cards.CardDataError, match=r"items: 1 items, need 2-4"):
        cards.validate("list", {"title": "t", "items": [{"text": "only"}]})
    with pytest.raises(cards.CardDataError, match=r"steps\[1\]: 19 characters, max 18"):
        cards.validate("flow", {"steps": ["ok", "x" * 19]})
    with pytest.raises(cards.CardDataError, match=r"right: required"):
        cards.validate("versus", {"left": {"label": "a", "value": "1"}})
    with pytest.raises(cards.CardDataError, match=r"left\.value: required"):
        cards.validate("versus", {"left": {"label": "a"}, "right": {"label": "b", "value": "2"}})


def test_unknown_template_is_an_error():
    with pytest.raises(cards.CardDataError, match="unknown template 'nope'"):
        cards.validate("nope", {})


def test_whitespace_is_normalised():
    out = cards.validate("quote", {"text": "  two\n words ", "who": "me"})
    assert out["text"] == "two words"


# --- the request contract ---------------------------------------------------------------------

def test_build_spec_template_defaults_to_the_card_canvas_and_duration():
    spec = app_module.build_spec({"template": "stat", "data": cards.TEMPLATES["stat"].example})
    assert (spec.width, spec.height, spec.fps) == (cards.CARD_WIDTH, cards.CARD_HEIGHT, 30)
    assert spec.duration == cards.TEMPLATES["stat"].duration
    assert spec.fmt == "mov"
    assert spec.at == spec.duration
    assert spec.frame_count == round(spec.duration * 30)
    assert spec.template == "stat"


def test_build_spec_raw_html():
    spec = app_module.build_spec({"html": "<p>hi</p>", "width": 200, "height": 100, "fps": 10,
                                  "duration": 1.5, "format": "png", "at": 0.5})
    assert spec.html == "<p>hi</p>"
    assert (spec.width, spec.height, spec.fps, spec.duration, spec.at) == (200, 100, 10, 1.5, 0.5)
    assert spec.template is None


@pytest.mark.parametrize("body, problem", [
    ({}, "exactly one of"),
    ({"template": "stat", "html": "<p>"}, "exactly one of"),
    ({"html": "   "}, "html: must be a non-empty string"),
    ({"template": "stat", "data": {}}, "value: required"),
    ({"template": "stat", "data": cards.TEMPLATES["stat"].example, "format": "gif"}, "format: must be one of"),
    ({"template": "stat", "data": cards.TEMPLATES["stat"].example, "theme": "pink"}, "theme: must be one of"),
    ({"html": "<p>x</p>", "theme": "neon"}, "theme: applies to templates"),
    ({"html": "<p>x</p><script>go()</script>"}, "scripts and event handlers are not allowed"),
    ({"html": '<img src="x" onerror="go()">'}, "scripts and event handlers are not allowed"),
    ({"html": "<p>x</p>", "format": "sheet", "frames": 40}, "frames: must be between"),
    ({"template": "stat", "data": cards.TEMPLATES["stat"].example, "width": 99999}, "width: must be between"),
    ({"template": "stat", "data": cards.TEMPLATES["stat"].example, "fps": "fast"}, "fps: must be a number"),
    ({"template": "stat", "data": cards.TEMPLATES["stat"].example, "duration": 0}, "duration: must be positive"),
    ([], "body must be a JSON object"),
])
def test_build_spec_rejects_bad_requests(body, problem):
    with pytest.raises((ValueError, cards.CardDataError), match=problem):
        app_module.build_spec(body)


# --- the routes, against a fake renderer -------------------------------------------------------

class FakeRenderer:
    """Returns one tiny PNG per requested frame and records what it was asked to capture."""
    version = "fake-chromium"
    PNG = bytes.fromhex("89504e470d0a1a0a")  # a PNG signature is enough for the routes

    def __init__(self):
        self.specs = []

    async def capture(self, page_id, spec):
        self.specs.append((page_id, spec))
        return [self.PNG] * len(spec.timestamps_ms)


@pytest.fixture
async def client(monkeypatch):
    renderer = FakeRenderer()
    service = app_module.Service(renderer)
    # The encoder needs the real frames; the routes only need to know it was called with them.
    monkeypatch.setattr(app_module, "encode_mov",
                        lambda frames, w, h, fps: b"MOV:%d:%dx%d@%d" % (len(frames), w, h, fps))
    monkeypatch.setattr(app_module, "compose_sheet",
                        lambda frames, stamps, w, h: b"SHEET:%d" % len(frames))
    async with TestClient(TestServer(app_module.build_app(service))) as c:
        c.renderer = renderer
        c.service = service
        yield c


async def test_health_and_templates(client):
    health = await (await client.get("/health")).json()
    assert health["ok"] and health["chromium"] == "fake-chromium"
    assert health["templates"] == sorted(cards.TEMPLATES)
    catalogue = await (await client.get("/templates")).json()
    assert set(catalogue["templates"]) == set(cards.TEMPLATES)
    assert catalogue["canvas"] == {"width": cards.CARD_WIDTH, "height": cards.CARD_HEIGHT}


async def test_render_template_returns_a_mov_with_frame_headers(client):
    resp = await client.post("/render", json={"template": "stat", "data": cards.TEMPLATES["stat"].example})
    assert resp.status == 200
    assert resp.content_type == "video/quicktime"
    frames = round(cards.TEMPLATES["stat"].duration * 30)
    assert resp.headers["X-Frames"] == str(frames)
    assert resp.headers["X-Fps"] == "30"
    assert await resp.read() == b"MOV:%d:%dx%d@30" % (frames, cards.CARD_WIDTH, cards.CARD_HEIGHT)
    page_id, spec = client.renderer.specs[-1]
    assert spec.template == "stat"
    # The page was stashed for the browser and consumed exactly once.
    assert client.service.take_page(page_id) is None


async def test_render_png_returns_one_frame(client):
    resp = await client.post("/render", json={"html": "<p>x</p>", "format": "png", "at": 0.3})
    assert resp.status == 200
    assert resp.content_type == "image/png"
    assert await resp.read() == FakeRenderer.PNG
    _, spec = client.renderer.specs[-1]
    assert spec.fmt == "png" and spec.at == 0.3


async def test_bad_data_is_a_400_naming_the_problem(client):
    resp = await client.post("/render", json={"template": "versus", "data": {"left": {"label": "a", "value": "1"}}})
    assert resp.status == 400
    body = await resp.json()
    assert body["ok"] is False
    assert "right: required" in body["error"]
    assert client.renderer.specs == []


async def test_malformed_json_is_a_400(client):
    resp = await client.post("/render", data=b"{not json", headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert "bad json" in (await resp.json())["error"]


async def test_pages_are_one_shot_and_loopback_only(client):
    page_id = client.service.stash_page("<p>page</p>")
    resp = await client.get(f"/_page/{page_id}")
    assert resp.status == 200 and await resp.text() == "<p>page</p>"
    assert (await client.get(f"/_page/{page_id}")).status == 404
    assert (await client.get("/_page/unknown")).status == 404


async def test_assets_are_served_without_directory_listing(client):
    assert (await client.get("/assets/cards.css")).status == 200
    assert (await client.get("/assets/fonts/PermanentMarker-Regular.ttf")).status == 200
    assert (await client.get("/assets/")).status in (403, 404)


# --- themes, the new templates, sheets and the design kit --------------------------------------

@pytest.mark.parametrize("theme", sorted(cards.THEMES))
@pytest.mark.parametrize("template_id", sorted(cards.TEMPLATES))
def test_every_template_renders_in_every_theme(template_id, theme):
    html, _ = cards.render_html(template_id, cards.TEMPLATES[template_id].example, theme)
    assert f'<body data-theme="{theme}">' in html


def test_every_theme_is_defined_in_the_stylesheet():
    css = (SERVICE_DIR / "assets" / "cards.css").read_text(encoding="utf-8")
    default, *others = cards.THEMES
    assert default == "paper"  # the :root tokens; every other theme overrides them
    for theme in others:
        assert f'[data-theme="{theme}"]' in css, theme


def test_bar_widths_are_proportional_and_leave_room_for_the_value():
    html, _ = cards.render_html("bars", {"bars": [
        {"label": "a", "value": "100", "size": "100"}, {"label": "b", "value": "25", "size": "25"}]})
    assert "--w:68.0" in html and "--w:17.0" in html


@pytest.mark.parametrize("template_id, data, problem", [
    ("bars", {"bars": [{"label": "a", "value": "x", "size": "lots"}, {"label": "b", "value": "y", "size": "2"}]},
     "bars[0].size: must be a plain number"),
    ("meter", {"percent": "140", "label": "x"}, "percent: must be between 0 and 100"),
    ("headline", {"text": "far too many words here"}, "need 1-3"),
    ("headline", {"text": "Incomprehensibilities"}, "max 12 at this size"),
])
def test_computed_fields_are_validated(template_id, data, problem):
    with pytest.raises(cards.CardDataError, match=re.escape(problem)):
        cards.render_html(template_id, data)


def test_meter_percentage_drives_the_ring_and_the_count():
    html, _ = cards.render_html("meter", {"percent": "82.6", "label": "share"})
    assert "--pct:83" in html and "--to:83" in html


def test_sheet_timestamps_spread_to_the_final_frame():
    spec = app_module.build_spec({"template": "stat", "data": cards.TEMPLATES["stat"].example,
                                  "format": "sheet", "frames": 4})
    stamps = spec.timestamps_ms
    assert len(stamps) == 4 and stamps[-1] == pytest.approx(spec.duration * 1000)
    assert stamps == sorted(stamps) and stamps[0] > 0


async def test_render_sheet_returns_one_png_of_all_frames(client):
    resp = await client.post("/render", json={"template": "meter", "theme": "neon", "format": "sheet",
                                              "data": cards.TEMPLATES["meter"].example})
    assert resp.status == 200 and resp.content_type == "image/png"
    assert await resp.read() == b"SHEET:%d" % app_module.SHEET_FRAMES_DEFAULT


async def test_design_route_serves_the_kit(client):
    body = await (await client.get("/design")).json()
    assert body["ok"] is True
    assert set(body["themes"]) == set(cards.THEMES)
    assert body["skeleton"] == kit.SKELETON
    assert set(body["examples"]) == set(kit.EXAMPLES)
    templates = await (await client.get("/templates")).json()
    assert templates["themes"] == cards.THEMES


def test_every_class_the_kit_names_exists_in_the_stylesheet():
    css = (SERVICE_DIR / "assets" / "cards.css").read_text(encoding="utf-8")
    named = set(kit.MOTION) | set(kit.COMPONENTS) | set(kit.TYPE_SCALE)
    missing = sorted(name for name in named if f".{name}" not in css)
    assert not missing, f"the kit documents classes the stylesheet does not define: {missing}"
    for token in kit.TOKENS:
        assert f"{token}:" in css, token


@pytest.mark.parametrize("name", sorted(kit.EXAMPLES))
def test_kit_examples_are_valid_bespoke_cards(name):
    example = kit.EXAMPLES[name]
    spec = app_module.build_spec({"html": example["html"], "duration": example["duration"], "format": "sheet"})
    assert spec.template is None and spec.duration == example["duration"]
    assert '/assets/cards.css' in example["html"] and "data-theme=" in example["html"]
