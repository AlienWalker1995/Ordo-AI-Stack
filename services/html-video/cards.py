"""The card template library: what each template needs, how its data is validated, and how it
becomes HTML.

A caller sends `{"template": "stat", "data": {...}}`; this module checks the data against the
template's declared fields (types, lengths, choices, list sizes) and renders the Jinja template
with autoescaping on, so the data can never inject markup. Every template declares its animation
`duration`: the renderer captures exactly that many seconds, after which the card holds its final
frame (every animation uses `animation-fill-mode: both`).

Adding a template = one HTML file under templates/, one `Template` entry here, and a test case
that its example validates and renders (tests/test_html_video.py iterates TEMPLATES).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# The card canvas: the reel composites it full-width across the top of a 1080x1920 frame. A
# template may be rendered at another size, but the layouts are designed for this one.
CARD_WIDTH = 1080
CARD_HEIGHT = 560


@dataclasses.dataclass(frozen=True)
class Field:
    """One string value. `choices` restricts it to an enumeration (the first choice is the
    default when the field is optional and absent)."""
    name: str
    description: str
    required: bool = True
    max_len: int = 60
    choices: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ObjectField:
    """A nested object of string fields (versus.left / versus.right)."""
    name: str
    description: str
    fields: tuple[Field, ...]
    required: bool = True


@dataclasses.dataclass(frozen=True)
class ListField:
    """A list of items: plain strings (`item_fields` empty) or objects of string fields."""
    name: str
    description: str
    min_items: int
    max_items: int
    item_fields: tuple[Field, ...] = ()
    item_max_len: int = 40
    required: bool = True


AnyField = Field | ObjectField | ListField


@dataclasses.dataclass(frozen=True)
class Template:
    id: str
    file: str
    description: str
    duration: float            # seconds of animation; the clip holds its last frame after this
    fields: tuple[AnyField, ...]
    example: dict[str, Any]


TEMPLATES: dict[str, Template] = {
    "stat": Template(
        id="stat", file="stat.html", duration=2.2,
        description="One big number with a label and an optional one-line note. The claim's "
                    "headline figure: a parameter count, a valuation, a benchmark score.",
        fields=(
            Field("value", "The number as it should read, units included: '501B', '$4.7B', '82%'.",
                  max_len=10),
            Field("label", "What the number is, 2-5 words: 'parameters', 'raised at $25B valuation'.",
                  max_len=30),
            Field("note", "The catch, one short line: 'only 23B active per token'.",
                  required=False, max_len=64),
            Field("stamp", "A rubber-stamp word over the card: 'CLAIMED', 'UNVERIFIED', 'SELF-GRADED'.",
                  required=False, max_len=14),
            Field("tone", "Colour of the number: neutral (ink), hype (blue), bust (red).",
                  required=False, max_len=8, choices=("neutral", "hype", "bust")),
        ),
        example={"value": "501B", "label": "parameters", "note": "only 23B wake up per token",
                 "stamp": "CLAIMED", "tone": "hype"},
    ),
    "versus": Template(
        id="versus", file="versus.html", duration=2.4,
        description="Two things side by side with a value each, and an optional verdict line. "
                    "The hype number against the real one.",
        fields=(
            Field("title", "What is being compared, 2-6 words.", required=False, max_len=40),
            ObjectField("left", "The left side.", fields=(
                Field("label", "Who or what.", max_len=20),
                Field("value", "Its number.", max_len=12),
            )),
            ObjectField("right", "The right side.", fields=(
                Field("label", "Who or what.", max_len=20),
                Field("value", "Its number.", max_len=12),
            )),
            Field("verdict", "The takeaway, one line, highlighted.", required=False, max_len=56),
        ),
        example={"title": "Compute behind the model", "left": {"label": "Mistral", "value": "4,000 GPUs"},
                 "right": {"label": "OpenAI", "value": "100,000+"},
                 "verdict": "a rounding error next to a data center"},
    ),
    "list": Template(
        id="list", file="list.html", duration=2.4,
        description="A titled checklist of 2-4 points, each marked true (check), false (x) or "
                    "unknown (question mark). Claims versus what is actually known.",
        fields=(
            Field("title", "The list's heading, 2-6 words.", max_len=40),
            ListField("items", "The points, in order.", min_items=2, max_items=4, item_fields=(
                Field("text", "The point, under ~7 words.", max_len=44),
                Field("mark", "check, x or question.", required=False, max_len=8,
                      choices=("check", "x", "question")),
            )),
        ),
        example={"title": "What Reflection actually showed", "items": [
            {"text": "501B total parameters", "mark": "check"},
            {"text": "beats GLM at 3-4x less compute", "mark": "question"},
            {"text": "independent benchmarks", "mark": "x"},
            {"text": "weights released", "mark": "x"},
        ]},
    ),
    "flow": Template(
        id="flow", file="flow.html", duration=2.4,
        description="2-4 boxes joined by arrows, drawn on left to right. A pipeline, a chain of "
                    "events, a money trail.",
        fields=(
            Field("title", "What the chain is, 2-6 words.", required=False, max_len=40),
            ListField("steps", "The boxes, left to right.", min_items=2, max_items=4, item_max_len=18),
        ),
        example={"title": "How the money moves", "steps": ["$4.7B raised", "501B model", "$25B valuation"]},
    ),
    "quote": Template(
        id="quote", file="quote.html", duration=2.2,
        description="A short quoted claim with who said it, and an optional stamp over it. The "
                    "exact words the hype rests on.",
        fields=(
            Field("text", "The claim, verbatim or lightly trimmed, under ~16 words.", max_len=100),
            Field("who", "Who said it: the company, the CEO, the press release.", max_len=32),
            Field("stamp", "A rubber-stamp word: 'UNVERIFIED', 'THEIR OWN TEST', 'FACT'.",
                  required=False, max_len=14),
        ),
        example={"text": "matches GLM-5 while using 3-4x less compute", "who": "Reflection AI, launch post",
                 "stamp": "SELF-GRADED"},
    ),
}


class CardDataError(ValueError):
    """The data does not fit the template. The message lists every problem, so a caller (an
    agent writing the data) can fix them all in one pass."""


def _check_str(value: Any, field: Field, where: str, problems: list[str]) -> str | None:
    if value is None:
        if field.required:
            problems.append(f"{where}: required")
        elif field.choices:
            return field.choices[0]
        return None
    if not isinstance(value, str):
        problems.append(f"{where}: must be a string")
        return None
    text = " ".join(value.split())  # collapse whitespace, strip ends
    if not text:
        if field.required:
            problems.append(f"{where}: must not be empty")
            return None
        return field.choices[0] if field.choices else None
    if len(text) > field.max_len:
        problems.append(f"{where}: {len(text)} characters, max {field.max_len}")
    if field.choices and text not in field.choices:
        problems.append(f"{where}: must be one of {', '.join(field.choices)}")
    return text


def _check_object(value: Any, fields: tuple[Field, ...], where: str, problems: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        problems.append(f"{where}: must be an object")
        return {}
    known = {f.name for f in fields}
    for key in value:
        if key not in known:
            problems.append(f"{where}.{key}: unknown field (known: {', '.join(sorted(known))})")
    return {f.name: _check_str(value.get(f.name), f, f"{where}.{f.name}", problems) for f in fields}


def validate(template_id: str, data: Any) -> dict[str, Any]:
    """Return the normalised data for `template_id`, or raise CardDataError naming every problem."""
    template = TEMPLATES.get(template_id)
    if template is None:
        raise CardDataError(f"unknown template {template_id!r} (known: {', '.join(sorted(TEMPLATES))})")
    if not isinstance(data, dict):
        raise CardDataError("data must be an object")
    problems: list[str] = []
    known = {f.name for f in template.fields}
    for key in data:
        if key not in known:
            problems.append(f"{key}: unknown field (known: {', '.join(sorted(known))})")
    out: dict[str, Any] = {}
    for field in template.fields:
        value = data.get(field.name)
        if isinstance(field, Field):
            out[field.name] = _check_str(value, field, field.name, problems)
        elif isinstance(field, ObjectField):
            if value is None:
                if field.required:
                    problems.append(f"{field.name}: required")
                out[field.name] = None
            else:
                out[field.name] = _check_object(value, field.fields, field.name, problems)
        elif isinstance(field, ListField):
            if value is None:
                if field.required:
                    problems.append(f"{field.name}: required")
                out[field.name] = []
                continue
            if not isinstance(value, list):
                problems.append(f"{field.name}: must be a list")
                out[field.name] = []
                continue
            if not field.min_items <= len(value) <= field.max_items:
                problems.append(f"{field.name}: {len(value)} items, need {field.min_items}-{field.max_items}")
            items = []
            for i, item in enumerate(value[:field.max_items]):
                where = f"{field.name}[{i}]"
                if field.item_fields:
                    items.append(_check_object(item, field.item_fields, where, problems))
                else:
                    items.append(_check_str(item, Field(where, "", max_len=field.item_max_len), where, problems))
            out[field.name] = items
    if problems:
        raise CardDataError("; ".join(problems))
    return out


_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(default=True))


def render_html(template_id: str, data: Any) -> tuple[str, Template]:
    """Validate `data` and return (html, template). Raises CardDataError."""
    normalised = validate(template_id, data)
    template = TEMPLATES[template_id]
    html = _env.get_template(template.file).render(d=normalised, template_id=template_id)
    return html, template


def _describe_field(field: AnyField) -> dict[str, Any]:
    if isinstance(field, Field):
        out: dict[str, Any] = {"type": "string", "required": field.required, "max_len": field.max_len,
                               "description": field.description}
        if field.choices:
            out["choices"] = list(field.choices)
        return out
    if isinstance(field, ObjectField):
        return {"type": "object", "required": field.required, "description": field.description,
                "fields": {f.name: _describe_field(f) for f in field.fields}}
    return {"type": "list", "required": field.required, "description": field.description,
            "min_items": field.min_items, "max_items": field.max_items,
            "item": ({"type": "object", "fields": {f.name: _describe_field(f) for f in field.item_fields}}
                     if field.item_fields else {"type": "string", "max_len": field.item_max_len})}


def describe() -> dict[str, Any]:
    """The template catalogue as JSON: what GET /templates returns, and what a caller (or an agent
    prompt) reads to know what it may send."""
    return {
        template.id: {
            "description": template.description,
            "duration": template.duration,
            "fields": {f.name: _describe_field(f) for f in template.fields},
            "example": template.example,
        }
        for template in TEMPLATES.values()
    }
