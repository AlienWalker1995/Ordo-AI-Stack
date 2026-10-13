# html-video: HTML/CSS/SVG animations to RGBA video

A CPU service that renders an HTML page with CSS animations to a short video clip with alpha
(PNG-in-MOV), to one still, or to a contact sheet of frames. Video pipelines use it for overlay
graphics: a concept card across the top of a vertical video, kinetic typography, a small diagram.
Callers are agents and scripts on the stack network; it publishes no host port.

Opt-in plugin (`default: false`): add `html-video` to `plugins:` in `ordo.yaml`, then:

```
ordo apply          # builds ordo/html-video, renders, starts it (profile media, port 8000)
```

## Why a browser, and why deterministic frames

Authoring a card as HTML + CSS is cheap and exact. ffmpeg's `drawtext` cannot lay out a card, and
an image model cannot be told precisely what to draw. Chromium can, and it can be stepped.

A screenshot loop against a live page would sample whatever state the animations happened to be
in. Instead the renderer pauses every animation on the page (CSS animations and transitions, Web
Animations, SVG SMIL) and sets its `currentTime` to the frame's timestamp before each capture.
Frame N is therefore the page at exactly N/fps seconds, however long the capture takes. Cards
animate with CSS only (a page with a script is refused), and every animation is
`animation-fill-mode: both`, so a clip holds its final frame after the last animation ends.

## API

| Route | What it does |
|---|---|
| `GET /health` | `{"ok": true, "chromium": "<version>", "templates": [...]}` |
| `GET /templates` | The template catalogue (fields, limits, choices, example data, duration) and the themes |
| `GET /design` | The design kit for bespoke cards: themes, tokens, type scale, motion classes, components, rules, a skeleton page and worked examples |
| `POST /render` | `{"template": "stat", "data": {...}, "theme": "chalk"}` or `{"html": "<!doctype html>...", "duration": 2.6}` |

`POST /render` options (all optional): `format` (`mov`, `png` for one frame, or `sheet`),
`width` and `height` (default 1080x560, the card canvas), `fps` (30), `duration` (a template's own;
seconds of animation to capture), `at` (png: the frame's timestamp; defaults to the end), `frames`
(sheet: how many moments, 2-12, default 6).

Responses: `video/quicktime` with `X-Frames`, `X-Fps` and `X-Duration` headers, or `image/png`.
Invalid input is a `400 {"ok": false, "error": "..."}` that names every problem at once, so an
agent can fix them all in one pass.

**Review before you render.** `format: "sheet"` returns one PNG with the card at several moments
across its animation (three across, each labelled with its timestamp). A caller that can see
images checks the motion, the layout and the final state in one look, fixes what is off, and
only then renders the clip.

```sh
curl -s http://html-video:8000/render -H 'content-type: application/json' \
  -d '{"template":"bars","theme":"blueprint","data":{"title":"GPUs behind each model","bars":[{"label":"OpenAI","value":"100,000+","size":"100000"},{"label":"Mistral","value":"4,000","size":"4000","focus":"yes"}]}}' \
  -o card.mov
ffmpeg -i video.mp4 -i card.mov -filter_complex "[1:v]format=rgba[c];[0:v][c]overlay=0:90" out.mp4
```

## Templates and themes

Templates are the fast path for common shapes. Callers send data, never markup (autoescaped).

| Template | Use |
|---|---|
| `stat` | One big number, a label, an optional note, stamp and tone |
| `versus` | Two labelled values side by side and a verdict |
| `list` | A titled checklist of 2-4 points, each marked check, x or question |
| `flow` | 2-4 boxes joined by arrows |
| `quote` | A short quoted claim, who said it, and an optional stamp |
| `bars` | 2-5 bars with lengths proportional to their sizes, one optionally in focus |
| `timeline` | 2-5 dated events on a line, one optionally in focus |
| `meter` | One percentage as a filling ring with a count-up |
| `headline` | Kinetic type: 1-3 huge words slammed in, with a kicker and a line below |

Every template renders in every theme (`theme`): `paper` (whiteboard sketch, the default), `chalk`
(chalkboard), `blueprint` (blue grid, clean lines) and `neon` (dark, glowing type). A theme only
redefines the colour tokens in `assets/cards.css`.

Adding a template: one HTML file under `templates/`, one `Template` entry in `cards.py` (fields,
example, and a `context` hook for values computed from the data), and nothing else: the tests
iterate the catalogue in every theme.

## Bespoke cards

When no template fits, a caller writes its own card on the same design system: `GET /design`
returns the skeleton page, the motion vocabulary (`write`, `pop`, `slam`, `grow-x`, `count`,
`draw`, ...), the components (`panel`, `chip`, `badge`, `stamp`, `circled`, ...), the type scale,
the tokens and the rules, with worked examples. Layout goes in the card's own `<style>`; colour,
type and motion come from the kit, which is what lets a bespoke card wear any theme. The tests
hold the kit to the stylesheet: every class it names exists.

## Trust boundary

The page is loaded from this same process over loopback, and every other request the page makes
is blocked, so a template or bespoke card can use only the bundled assets: no network, no files.
Scripts, `javascript:` URLs and inline event handlers are refused before anything renders.
Chromium runs as the image's unprivileged user without its setuid sandbox (it needs a capability
the stack does not grant). The service sits on the stack network only; it publishes no host port.

## Pinning

The base image is Microsoft's Playwright image for Python, pinned by digest. It ships the
browsers but not the `playwright` package, so the Dockerfile installs the package at the same
release as the image's browser revision; bump both together. The encoder is PyAV's wheel with
FFmpeg bundled, pinned with pip like every other dependency. Fonts are vendored under
`assets/fonts/` with their licences (Permanent Marker, Apache 2.0; Patrick Hand, OFL 1.1).
