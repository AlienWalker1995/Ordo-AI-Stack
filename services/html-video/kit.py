"""The design kit for bespoke cards: what GET /design returns.

The templates cover the common shapes (a number, a comparison, a list...). A caller that wants
something they do not cover writes its own HTML card on the same design system: the shared
stylesheet (assets/cards.css), its themes, its motion vocabulary and its components. This module
is the contract for that: every class it names exists in the stylesheet (tests hold it to that),
the skeleton is a complete page, and each example is a real card that renders.

A bespoke card is sent as `{"html": "...", "duration": <seconds>}` to POST /render. Render it as
`"format": "sheet"` first to see several frames of the motion in one image.
"""
from __future__ import annotations

from typing import Any

import cards

# The part every bespoke card starts from. Fill in the theme, the card class and the body.
SKELETON = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="/assets/cards.css">
<style>
  /* Layout only, for this card. Colours, fonts and sizes come from the tokens and classes. */
</style>
</head>
<body data-theme="paper">
<svg width="0" height="0" aria-hidden="true" style="position:absolute">
  <defs>
    <filter id="rough" x="-5%" y="-5%" width="110%" height="110%">
      <feTurbulence type="fractalNoise" baseFrequency="0.007" numOctaves="1" seed="7" result="noise"/>
      <feDisplacementMap in="SourceGraphic" in2="noise" scale="8" xChannelSelector="R" yChannelSelector="G"/>
    </filter>
  </defs>
</svg>
<div class="stage">
  <div class="card">
    <svg class="frame" viewBox="0 0 976 476" aria-hidden="true"><rect x="4" y="4" width="968" height="468" rx="22"/></svg>
    <div class="body">
      <!-- the card's content: elements with a motion class and an inline --d delay -->
    </div>
  </div>
</div>
</body>
</html>
"""

MOTION: dict[str, str] = {
    "write": "Wipes the element in left to right, like handwriting. Text lines, titles.",
    "pop": "Scales up from 55% with a small overshoot. Numbers, badges, boxes.",
    "fade": "Fades in. Secondary text, labels.",
    "rise": "Slides up 48px and fades in. Rows, stacked items.",
    "slide-left": "Slides in from the left. The first of two things being compared.",
    "slide-right": "Slides in from the right. The second of two things being compared.",
    "slam": "Drops in from 2.6x with a hard landing and a slight tilt. One-word verdicts.",
    "shake": "Shakes sideways once. Apply after an element has appeared, to call it out.",
    "blink": "Blinks twice. A warning, a cursor.",
    "grow-x": "Grows from the left edge. Bars, underlines, rails. Set its width in your <style>.",
    "grow-y": "Grows up from the bottom edge. Columns.",
    "draw": "Draws an SVG stroke on (a path, line, polyline or ellipse). Give the shape pathLength=\"1200\" so it draws across the whole animation.",
    "count": "Counts an integer up from 0: <span class=\"count\" style=\"--to:83;--d:.6s\"></span>.",
    "type": "Types a one-line string character by character; set --chars to its length.",
    "hl": "Highlighter swipe behind a phrase: <span class=\"hl\" style=\"--d:1.2s\">phrase</span>.",
}

COMPONENTS: dict[str, str] = {
    "card": "The card surface, 1000x500 at the centre of the canvas. Add `tilt-right` or `flat` "
            "to change its slight default tilt.",
    "frame": "The ink outline that draws itself around the card (the <svg class=\"frame\"> in the skeleton).",
    "panel": "A box on the card with an ink border.",
    "chip": "A small pill label, inverted colours.",
    "badge": "A round tilted badge, e.g. VS.",
    "stamp": "A rubber stamp slammed onto the card's bottom-right corner. Put it after .body, "
             "inside .card, with its own --d.",
    "circled": "A hand-drawn circle around a word: <span class=\"circled\">word<svg viewBox=\"0 0 100 40\" "
               "preserveAspectRatio=\"none\"><ellipse class=\"draw\" pathLength=\"1200\" cx=\"50\" cy=\"20\" rx=\"48\" ry=\"18\" "
               "style=\"--d:1s\"/></svg></span>.",
    "muted": "Secondary text colour.",
    "good": "The good colour (green family in every theme).",
    "bad": "The bad colour (red family).",
    "hype": "The hype colour (blue family).",
}

TYPE_SCALE: dict[str, str] = {
    "huge": "200px display, 1-2 short words.",
    "big": "168px display, one number.",
    "large": "112px display, a short phrase of up to ~13 characters.",
    "title": "52px display, a heading.",
    "label": "60px handwriting, the main line.",
    "note": "44px handwriting, muted, a secondary line.",
    "small": "34px handwriting.",
}

TOKENS: dict[str, str] = {
    "--paper": "card surface", "--panel": "boxes on the card", "--ink": "primary text and strokes",
    "--ink-soft": "secondary text", "--accent": "highlighter, quote marks", "--accent-2": "second highlight",
    "--good": "good", "--bad": "bad", "--hype": "hype", "--display": "display font (marker)",
    "--hand": "body font (handwriting)", "--glow": "text-shadow for display type (only neon glows)",
}

RULES: tuple[str, ...] = (
    "Animate with CSS only: no <script> (it is refused). The renderer pauses every CSS and SVG "
    "animation and steps it to each frame's timestamp, which is why the output is exact.",
    "Every animated element gets an inline --d delay. Stagger them so the eye reads the card in "
    "order; 0.2-0.4s between elements reads well.",
    "Send `duration` = the last element's delay plus its animation (~0.5s) plus ~0.3s of hold. "
    "Most cards land between 2 and 3.2 seconds.",
    "Use the tokens and classes for colour, font and size; write only layout (grid, flex, "
    "positions, widths) in your own <style>. That is what makes the themes work.",
    "Keep everything inside the card (1000x500, padding 38px 56px). Text on a card is read in a few "
    "seconds on a phone: under ~14 words, nothing below the `small` size.",
    "Only /assets/ URLs load (the stylesheet and the two fonts). Draw everything else with HTML, "
    "CSS and inline SVG.",
    "One idea per card. Pick the form from the content: a comparison wants two things side by "
    "side, a trend wants a line, a claim wants the words, a verdict wants one huge word.",
)

# Real bespoke cards, rendered by the tests' validation and by hand: starting points, not menus.
EXAMPLES: dict[str, dict[str, Any]] = {
    "crossed-out-claim": {
        "description": "The claim in big type, struck through by a hand-drawn line, with the "
                       "correction written underneath.",
        "duration": 2.6,
        "html": SKELETON.replace('<body data-theme="paper">', '<body data-theme="chalk">').replace(
            "  /* Layout only, for this card. Colours, fonts and sizes come from the tokens and classes. */",
            "  .body { justify-content: center; }\n"
            "  .claim { position: relative; align-self: flex-start; white-space: nowrap; margin-top: 26px; }\n"
            "  .claim svg { position: absolute; left: -14px; top: 44%; width: calc(100% + 28px); height: 24px; overflow: visible; }\n"
            "  .claim path { fill: none; stroke: var(--bad); stroke-width: 14; stroke-linecap: round; }\n"
            "  .fix { margin-top: 30px; }",
        ).replace(
            "      <!-- the card's content: elements with a motion class and an inline --d delay -->",
            '      <div class="chip fade" style="--d:.35s">THE CLAIM</div>\n'
            '      <div class="claim large write" style="--d:.55s">"open source"\n'
            '        <svg viewBox="0 0 400 24" preserveAspectRatio="none"><path class="draw" pathLength="1200" d="M6 15 C120 6 280 20 394 9" style="--d:1.3s"/></svg>\n'
            "      </div>\n"
            '      <div class="fix label write" style="--d:1.75s">weights: <span class="hl" style="--d:2.1s">not released</span></div>',
        ),
    },
    "two-scales": {
        "description": "Two circles whose areas compare two quantities, each labelled, with a "
                       "one-line takeaway. Good for 'X is tiny next to Y'.",
        "duration": 2.8,
        "html": SKELETON.replace('<body data-theme="paper">', '<body data-theme="blueprint">').replace(
            '<div class="card">', '<div class="card flat">').replace(
            "  /* Layout only, for this card. Colours, fonts and sizes come from the tokens and classes. */",
            "  .row { flex: 1; display: flex; align-items: flex-end; justify-content: center; gap: 120px; }\n"
            "  .unit { display: flex; flex-direction: column; align-items: center; gap: 6px; }\n"
            "  .dot { border-radius: 50%; background: var(--hype); margin-bottom: 10px; }\n"
            "  /* Areas in proportion: 4,000 / 100,000 = 0.04, so the diameter ratio is 0.2. */\n"
            "  .dot.small-dot { width: 42px; height: 42px; }\n"
            "  .dot.big-dot { width: 210px; height: 210px; background: var(--ink-soft); }\n"
            "  .takeaway { text-align: center; margin-top: 18px; }",
        ).replace(
            "      <!-- the card's content: elements with a motion class and an inline --d delay -->",
            '      <div class="row">\n'
            '        <div class="unit"><div class="dot small-dot pop" style="--d:.6s"></div>'
            '<div class="title fade" style="--d:.9s">4,000</div><div class="small muted fade" style="--d:1s">their GPUs</div></div>\n'
            '        <div class="unit"><div class="dot big-dot pop" style="--d:1.1s"></div>'
            '<div class="title fade" style="--d:1.4s">100,000</div><div class="small muted fade" style="--d:1.5s">the leader\'s</div></div>\n'
            "      </div>\n"
            '      <div class="takeaway label write" style="--d:1.9s">a <span class="hl" style="--d:2.3s">rounding error</span></div>',
        ),
    },
}


def describe() -> dict[str, Any]:
    """The kit as JSON (GET /design)."""
    return {
        "canvas": {"width": cards.CARD_WIDTH, "height": cards.CARD_HEIGHT,
                   "card": {"width": 1000, "height": 500, "padding": "38px 56px"},
                   "note": "The canvas is transparent around the card; the video keeps the alpha."},
        "how": ("Start from `skeleton`, set <body data-theme>, lay out the card's content with "
                "the classes below, then POST /render {\"html\": ..., \"duration\": ..., "
                "\"format\": \"sheet\"} to see the motion in one image. Fix what looks off, then "
                "render the video. Templates (GET /templates) are the fast path for the common shapes."),
        "themes": cards.THEMES,
        "tokens": TOKENS,
        "type_scale": TYPE_SCALE,
        "motion": MOTION,
        "components": COMPONENTS,
        "rules": list(RULES),
        "skeleton": SKELETON,
        "examples": EXAMPLES,
    }
