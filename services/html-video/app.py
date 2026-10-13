"""html-video: an HTTP service that renders an HTML/CSS/SVG animation to an RGBA video clip.

Why a browser: overlay graphics for video (sketch-style concept cards, kinetic
typography, small diagrams) are cheap to author as HTML + CSS animation and expensive any other
way. ffmpeg's drawtext cannot lay out a card; an image model cannot be told exactly what to draw.
Chromium can render the page, and it can be stepped frame by frame.

Why the frames are deterministic: a screenshot loop against a live page would sample whatever
state the animations happened to be in. Instead, every animation on the page (CSS animations and
transitions, Web Animations, SVG SMIL) is paused and its `currentTime` is set to the frame's
timestamp before each capture (`SEEK_JS`), so frame N is the page at exactly N/fps seconds, no
matter how long the capture takes. Templates therefore animate with CSS only (no JS timers).

Routes:
  GET  /health            {"ok": true, "chromium": "<version>", "templates": [...]}
  GET  /templates         the template catalogue (fields, limits, example data), see cards.py
  GET  /design            the design kit for bespoke cards (themes, motion, components, rules,
                          skeleton, examples), see kit.py
  POST /render            {"template": "stat", "data": {...}, "theme": "chalk"}
                          or {"html": "<!doctype html>...", "duration": 2.5}
                          plus optional width, height, fps, duration, format, at, frames
                          format "mov"   -> video/quicktime (PNG-in-MOV, RGBA)
                          format "png"   -> image/png, one frame at `at`
                          format "sheet" -> image/png, `frames` frames across the animation in
                                            one labelled grid: the motion at a glance
                          -> 400 {"ok": false, "error": "..."} when the request or data is invalid
  GET  /_page/<id>        the page the browser loads for one render (loopback only, one-shot)
  GET  /assets/...        the stylesheet and fonts the templates reference

The page is loaded from this same server over loopback, and every other request the page makes
is blocked, so a template (or raw html) can use only the bundled assets: no network, no files.
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import cards
import kit
from aiohttp import web

log = logging.getLogger("html-video")

HERE = Path(__file__).resolve().parent
ASSETS_DIR = HERE / "assets"
PORT = int(os.environ.get("HTML_VIDEO_PORT", "8000"))
# Guards on what one request may ask for. A card is a few seconds; these bound memory (frames
# are held in RAM until encoded) rather than describe a use.
MAX_DIMENSION = 2160
MAX_FPS = 60
MAX_FRAMES = 1800
MAX_BODY_BYTES = 2 * 1024 * 1024
FORMATS = ("mov", "png", "sheet")
SHEET_FRAMES_DEFAULT = 6
SHEET_FRAMES_MAX = 12
# Raw html must animate with CSS: a script's timers and requestAnimationFrame do not follow the
# renderer's seek, so their frames would not be the page at N/fps seconds.
_SCRIPT_RE = re.compile(r"<\s*script\b|\bjavascript\s*:|\son[a-z]+\s*=", re.IGNORECASE)
CONCURRENCY = int(os.environ.get("HTML_VIDEO_CONCURRENCY", "2"))
PAGE_TTL_S = 120.0

# Runs in the page before the first capture: pause everything, and expose a seek.
PREPARE_JS = """
() => {
  const seek = (ms) => {
    for (const a of document.getAnimations()) { a.pause(); a.currentTime = ms; }
    for (const s of document.querySelectorAll('svg')) {
      if (s.pauseAnimations) { s.pauseAnimations(); s.setCurrentTime(ms / 1000); }
    }
  };
  seek(0);
  window.__htmlVideoSeek = seek;
  return document.getAnimations().length;
}
"""
SEEK_JS = "(ms) => window.__htmlVideoSeek(ms)"


@dataclasses.dataclass(frozen=True)
class RenderSpec:
    """One validated render request."""
    html: str
    width: int
    height: int
    fps: int
    duration: float
    fmt: str                 # "mov" (video, RGBA) | "png" (one frame) | "sheet" (frames in a grid)
    at: float                # png only: the timestamp of the captured frame
    template: str | None     # None for raw html
    sheet_frames: int = SHEET_FRAMES_DEFAULT

    @property
    def frame_count(self) -> int:
        return max(1, int(round(self.duration * self.fps)))

    @property
    def timestamps_ms(self) -> list[float]:
        """The timestamps to capture: every frame of a video, `at` for a still, and for a sheet
        `sheet_frames` moments spread across the animation, the last one its final frame."""
        if self.fmt == "png":
            return [self.at * 1000.0]
        if self.fmt == "sheet":
            n = self.sheet_frames
            return [self.duration * 1000.0 * (i + 1) / n for i in range(n)]
        return [i * 1000.0 / self.fps for i in range(self.frame_count)]


def _number(body: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key}: must be a number")
    if not lo <= value <= hi:
        raise ValueError(f"{key}: must be between {lo:g} and {hi:g}")
    return float(value)


def build_spec(body: Any) -> RenderSpec:
    """Turn a request body into a RenderSpec, or raise ValueError with the reason (-> 400).
    Pure: no browser involved, so the request contract is unit-testable."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    template_id = body.get("template")
    raw_html = body.get("html")
    if (template_id is None) == (raw_html is None):
        raise ValueError("send exactly one of `template` (+ `data`) or `html`")
    duration_default = 2.0
    theme = body.get("theme")
    if template_id is not None:
        if not isinstance(template_id, str):
            raise ValueError("template: must be a string")
        if theme is not None and not isinstance(theme, str):
            raise ValueError("theme: must be a string")
        html, template = cards.render_html(template_id, body.get("data", {}), theme)
        duration_default = template.duration
    else:
        if not isinstance(raw_html, str) or not raw_html.strip():
            raise ValueError("html: must be a non-empty string")
        if theme is not None:
            raise ValueError("theme: applies to templates; a bespoke card sets <body data-theme=\"...\">")
        if _SCRIPT_RE.search(raw_html):
            raise ValueError("html: scripts and event handlers are not allowed; animate with CSS "
                             "(see GET /design): the renderer steps CSS and SVG animations frame by frame")
        html = raw_html
    fmt = body.get("format", "mov")
    if fmt not in FORMATS:
        raise ValueError(f"format: must be one of {', '.join(FORMATS)}")
    width = int(_number(body, "width", cards.CARD_WIDTH, 16, MAX_DIMENSION))
    height = int(_number(body, "height", cards.CARD_HEIGHT, 16, MAX_DIMENSION))
    fps = int(_number(body, "fps", 30, 1, MAX_FPS))
    duration = _number(body, "duration", duration_default, 0.0, MAX_FRAMES / fps)
    if fmt in ("mov", "sheet") and duration <= 0:
        raise ValueError(f"duration: must be positive for a {'video' if fmt == 'mov' else 'sheet'}")
    at = _number(body, "at", duration, 0.0, MAX_FRAMES / fps)
    sheet_frames = int(_number(body, "frames", SHEET_FRAMES_DEFAULT, 2, SHEET_FRAMES_MAX))
    return RenderSpec(html=html, width=width, height=height, fps=fps, duration=duration,
                      fmt=fmt, at=at, template=template_id, sheet_frames=sheet_frames)


def compose_sheet(frames: list[bytes], timestamps_ms: list[float], width: int, height: int) -> bytes:
    """Frames -> one PNG grid (three across), each frame at half size on a neutral grey with its
    timestamp, so a reviewer sees the card's motion and its final state in one image."""
    from PIL import Image, ImageDraw, ImageFont  # imported here for the same reason as in encode_mov

    cols = min(3, len(frames))
    rows = -(-len(frames) // cols)
    cell_w, cell_h, gap, label_h = width // 2, height // 2, 12, 28
    sheet = Image.new("RGB", (cols * cell_w + (cols + 1) * gap, rows * (cell_h + label_h) + (rows + 1) * gap),
                      (128, 128, 128))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=20)
    for i, (png, ms) in enumerate(zip(frames, timestamps_ms, strict=True)):
        x = gap + (i % cols) * (cell_w + gap)
        y = gap + (i // cols) * (cell_h + label_h + gap)
        with Image.open(io.BytesIO(png)) as image:
            frame = image.convert("RGBA").resize((cell_w, cell_h))
        sheet.paste(frame, (x, y + label_h), frame)
        draw.text((x + 4, y + 3), f"{ms / 1000.0:.2f}s", fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    sheet.save(out, format="PNG")
    return out.getvalue()


def encode_mov(frames: list[bytes], width: int, height: int, fps: int) -> bytes:
    """PNG screenshots -> a QuickTime file whose video track is PNG frames with alpha. Lossless,
    and any ffmpeg decodes it (the png decoder is built in), which is what a video compositor
    needs: `-i card.mov` then `overlay` keeps the transparency."""
    # Imported here, not at module level: the request contract (build_spec) and the routes are
    # unit-tested without the encoder's binary wheels installed.
    import av
    import numpy as np
    from PIL import Image

    buf = io.BytesIO()
    with av.open(buf, mode="w", format="mov") as container:
        stream = container.add_stream("png", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "rgba"
        for png in frames:
            with Image.open(io.BytesIO(png)) as image:
                rgba = np.asarray(image.convert("RGBA"))
            frame = av.VideoFrame.from_ndarray(rgba, format="rgba")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buf.getvalue()


class ChromiumRenderer:
    """One headless Chromium for the process; a fresh browser context per render."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")   # this server, over loopback: the only origin a page may load
        self._playwright = None
        self._browser = None
        self.version = ""

    async def start(self) -> None:
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--font-render-hinting=none", "--disable-lcd-text", "--hide-scrollbars",
                  "--disable-gpu", "--no-sandbox"])
        self.version = self._browser.version
        log.info("chromium %s ready", self.version)

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def capture(self, page_id: str, spec: RenderSpec) -> list[bytes]:
        """The frames of `spec` as PNG bytes, one per `spec.timestamps_ms`."""
        assert self._browser is not None, "renderer not started"
        own_origin = self._base_url + "/"
        context = await self._browser.new_context(
            viewport={"width": spec.width, "height": spec.height}, device_scale_factor=1,
            reduced_motion="no-preference")
        try:
            page = await context.new_page()
            # Only this server's own pages and assets: a template cannot reach the network.
            async def own_assets_only(route):
                if route.request.url.startswith(own_origin):
                    await route.continue_()
                else:
                    await route.abort()
            await page.route("**/*", own_assets_only)
            await page.goto(f"{self._base_url}/_page/{page_id}", wait_until="load")
            await page.evaluate("document.fonts.ready")
            await page.evaluate(PREPARE_JS)
            frames: list[bytes] = []
            for ms in spec.timestamps_ms:
                await page.evaluate(SEEK_JS, ms)
                frames.append(await page.screenshot(
                    type="png", omit_background=True, animations="allow", caret="hide",
                    clip={"x": 0, "y": 0, "width": spec.width, "height": spec.height}))
            return frames
        finally:
            await context.close()


class Service:
    """The HTTP application state: the renderer, the one-shot page store, the concurrency gate."""

    def __init__(self, renderer: ChromiumRenderer) -> None:
        self.renderer = renderer
        self._pages: dict[str, tuple[str, float]] = {}
        self._gate = asyncio.Semaphore(CONCURRENCY)

    def stash_page(self, html: str) -> str:
        now = time.monotonic()
        for page_id, (_, expires) in list(self._pages.items()):
            if expires < now:
                del self._pages[page_id]
        page_id = secrets.token_urlsafe(12)
        self._pages[page_id] = (html, now + PAGE_TTL_S)
        return page_id

    def take_page(self, page_id: str) -> str | None:
        entry = self._pages.pop(page_id, None)
        return entry[0] if entry else None

    async def render(self, spec: RenderSpec) -> bytes:
        page_id = self.stash_page(spec.html)
        async with self._gate:
            started = time.monotonic()
            try:
                frames = await self.renderer.capture(page_id, spec)
            finally:
                self._pages.pop(page_id, None)  # the browser took it, or never got to it
            captured = time.monotonic()
            if spec.fmt == "png":
                out = frames[0]
            elif spec.fmt == "sheet":
                out = await asyncio.to_thread(compose_sheet, frames, spec.timestamps_ms, spec.width, spec.height)
            else:
                out = await asyncio.to_thread(encode_mov, frames, spec.width, spec.height, spec.fps)
        log.info("render %s %dx%d %d frames: capture %.1fs encode %.1fs -> %d bytes",
                 spec.template or "html", spec.width, spec.height, len(frames),
                 captured - started, time.monotonic() - captured, len(out))
        return out


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def build_app(service: Service) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "chromium": service.renderer.version,
                                  "templates": sorted(cards.TEMPLATES)})

    async def templates(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "templates": cards.describe(), "themes": cards.THEMES,
                                  "canvas": {"width": cards.CARD_WIDTH, "height": cards.CARD_HEIGHT}})

    async def design(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, **kit.describe()})

    async def render(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception as exc:  # malformed JSON
            return _error(400, f"bad json: {exc}")
        try:
            spec = build_spec(body)
        except (ValueError, cards.CardDataError) as exc:
            return _error(400, str(exc))
        try:
            out = await service.render(spec)
        except Exception:  # a browser failure is a 500 with the log, never a silent empty clip
            log.exception("render failed")
            return _error(500, "render failed; see the service log")
        if spec.fmt in ("png", "sheet"):
            return web.Response(body=out, content_type="image/png")
        return web.Response(body=out, content_type="video/quicktime",
                            headers={"X-Frames": str(spec.frame_count), "X-Duration": f"{spec.duration:.3f}",
                                     "X-Fps": str(spec.fps)})

    async def page(request: web.Request) -> web.Response:
        # The browser inside this container is the only legitimate reader.
        if request.remote not in ("127.0.0.1", "::1"):
            return _error(403, "pages are served to the local browser only")
        html = service.take_page(request.match_info["page_id"])
        if html is None:
            return _error(404, "unknown or expired page")
        return web.Response(text=html, content_type="text/html")

    app.router.add_get("/health", health)
    app.router.add_get("/templates", templates)
    app.router.add_get("/design", design)
    app.router.add_post("/render", render)
    app.router.add_get("/_page/{page_id}", page)
    app.router.add_static("/assets", ASSETS_DIR, show_index=False)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    renderer = ChromiumRenderer(f"http://127.0.0.1:{PORT}")
    service = Service(renderer)
    app = build_app(service)

    async def on_startup(_app: web.Application) -> None:
        await renderer.start()

    async def on_cleanup(_app: web.Application) -> None:
        await renderer.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
