# html-video: HTML/CSS/SVG animations to RGBA video

A CPU service that renders an HTML page with CSS animations to a short video clip with alpha
(PNG-in-MOV), or to one still. The reel pipelines use it for overlay graphics: the sketch-style
concept cards across the top of the Rick and Morty dialogue reel. Anything a browser can draw and
animate with CSS is a candidate: kinetic typography, a small diagram, a lower third.

Build and run:

```
ordo build html-video
ordo up html-video          # profile media, service name html-video, port 8000
```

## Why a browser, and why deterministic frames

Authoring a card as HTML + CSS is cheap and exact. ffmpeg's `drawtext` cannot lay out a card, and
an image model cannot be told precisely what to draw. Chromium can, and it can be stepped.

A screenshot loop against a live page would sample whatever state the animations happened to be
in. Instead the renderer pauses every animation on the page (CSS animations and transitions, Web
Animations, SVG SMIL) and sets its `currentTime` to the frame's timestamp before each capture.
Frame N is therefore the page at exactly N/fps seconds, however long the capture takes.
Templates animate with CSS only, and every animation is `animation-fill-mode: both`, so a clip
holds its final frame after the last animation ends.

## API

| Route | What it does |
|---|---|
| `GET /health` | `{"ok": true, "chromium": "<version>", "templates": [...]}` |
| `GET /templates` | The template catalogue: fields, limits, choices, example data, animation duration |
| `POST /render` | Render. Body: `{"template": "stat", "data": {...}}` or `{"html": "<!doctype html>..."}` |

`POST /render` options (all optional): `width` and `height` (default 1080x560, the card canvas),
`fps` (30), `duration` (the template's own; seconds of animation to capture), `format` (`mov`, or
`png` for one frame), `at` (png only, the frame's timestamp; defaults to the end).

Responses: `video/quicktime` with `X-Frames`, `X-Fps` and `X-Duration` headers, or `image/png`.
Invalid data is a `400 {"ok": false, "error": "..."}` that names every problem at once, so an
agent that wrote the data can fix them in one pass.

```sh
curl -s http://html-video:8000/render -H 'content-type: application/json' \
  -d '{"template":"stat","data":{"value":"501B","label":"parameters","note":"only 23B wake up per token","stamp":"CLAIMED","tone":"hype"}}' \
  -o card.mov
ffmpeg -i reel.mp4 -i card.mov -filter_complex "[1:v]format=rgba[c];[0:v][c]overlay=0:90" out.mp4
```

## Templates

The library lives in `cards.py` (the schema and validation) and `templates/*.html` (the markup),
on one stylesheet, `assets/cards.css`, which holds every design token. Callers send data, never
markup: the templates are autoescaped.

| Template | Use |
|---|---|
| `stat` | One big number, a label, an optional note, stamp and tone |
| `versus` | Two labelled values side by side (left = the claim, right = the reality) and a verdict |
| `list` | A titled checklist of 2-4 points, each marked check, x or question |
| `flow` | 2-4 boxes joined by arrows, drawn on left to right |
| `quote` | A short quoted claim, who said it, and an optional stamp |

Adding a template: one HTML file under `templates/`, one `Template` entry in `cards.py`, and the
example renders in `tests/test_html_video.py` (it iterates the catalogue).

Fonts are vendored under `assets/fonts/` with their licences (Permanent Marker, Apache 2.0;
Patrick Hand, OFL 1.1). Templates reference them through `/assets/`, served by this process.

## Trust boundary

The page is loaded from this same process over loopback, and every other request the page makes
is blocked, so a template (or raw `html`) can use only the bundled assets: no network, no files.
Chromium runs as the image's unprivileged user without its setuid sandbox (it needs a capability
the stack does not grant). The service sits on the stack network only; it publishes no host port.

## Pinning

The base image is Microsoft's Playwright image for Python, pinned by digest. It ships the
browsers but not the `playwright` package, so the Dockerfile installs the package at the same
release as the image's browser revision; bump both together. The encoder is PyAV's wheel with
FFmpeg bundled, pinned with pip like every other dependency.
