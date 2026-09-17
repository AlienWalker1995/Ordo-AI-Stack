"""Exact tool-call checking for model_toolcall (pure: no model, no framework).

An item declares the tools it offered (OpenAI `tools` format) and the calls it expects:

    "expected": {"calls": [{"name": "get_weather", "arguments": {"city": "Paris"}}], "order": "any"}

`calls: []` means NO tool call is correct (the model must answer in text).

A call passes only when BOTH layers hold:
  1. SCHEMA  - the call names an offered tool, its arguments parsed, every required parameter is
               present, no parameter outside the schema, and every value has the schema type and
               respects `enum` (recursively for arrays and objects). An integer parameter rejects
               "5" and 5.5; a number parameter accepts 5 and 5.5; a boolean never accepts 0 or 1.
  2. VALUES  - every argument matches its expected spec, and there is no argument the item did not
               list:
        literal string        equal after strip() + casefold() (BFCL's AST checker does the same)
        literal number        numerically equal (5 == 5.0)
        literal bool / null   identical
        list                  same length, element-wise in order
        object                same keys, recursively
        {"one_of": [a, b]}    matches any alternative
        {"unordered": [...]}  a list matching these elements in any order
        {"exact": "Str"}      string compared without case-folding
        {"any": true}         present, any (schema-valid) value
        {"optional": spec}    may be absent; when present it must match spec

With several expected calls, `order: "any"` (the default, for parallel calls) accepts any one-to-one
pairing; `order: "exact"` requires the same sequence. The call COUNT must match either way.
"""
from __future__ import annotations

import itertools
from typing import Any

_SPEC_KEYS = {"one_of", "unordered", "exact", "any", "optional"}


def _is_spec(value: Any) -> bool:
    return isinstance(value, dict) and len(value) == 1 and next(iter(value)) in _SPEC_KEYS


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": _is_integer,
    "number": _is_number,
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


# ── layer 1: schema ────────────────────────────────────────────────────────────

def schema_errors(value: Any, schema: dict[str, Any], path: str = "") -> list[str]:
    """Type, enum, required and unknown-key violations of `value` against a JSON-schema subset."""
    label = path or "arguments"
    if "enum" in schema and value not in schema["enum"]:
        return [f"{label}: {value!r} not in enum {schema['enum']}"]
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else ([declared] if declared else [])
    if types and not any(_TYPE_CHECKS.get(t, lambda v: True)(value) for t in types):
        return [f"{label}: {value!r} is not of type {declared}"]

    errors: list[str] = []
    if isinstance(value, dict):
        properties = schema.get("properties", {}) or {}
        for name in schema.get("required", []) or []:
            if name not in value:
                errors.append(f"{label}: missing required parameter {name!r}")
        additional = schema.get("additionalProperties", False)
        for name, item in value.items():
            child = f"{path}.{name}" if path else name
            if name in properties:
                errors.extend(schema_errors(item, properties[name], child))
            elif not additional:
                errors.append(f"{label}: unexpected parameter {name!r}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(schema_errors(item, schema["items"], f"{label}[{index}]"))
    return errors


# ── layer 2: values ────────────────────────────────────────────────────────────

def _unordered_matches(options: list[Any], got: Any) -> bool:
    if not isinstance(got, list) or len(got) != len(options):
        return False
    remaining = list(got)
    for option in options:
        for index, candidate in enumerate(remaining):
            if value_matches(option, candidate):
                del remaining[index]
                break
        else:
            return False
    return True


def value_matches(expected: Any, got: Any) -> bool:
    """True when `got` satisfies the expected value or spec (see the module docstring)."""
    if _is_spec(expected):
        key, spec = next(iter(expected.items()))
        if key == "any":
            return True
        if key == "optional":
            return value_matches(spec, got)
        if key == "one_of":
            return any(value_matches(option, got) for option in spec)
        if key == "exact":
            return isinstance(got, str) and got.strip() == str(spec).strip()
        return _unordered_matches(spec, got)  # key == "unordered"
    if isinstance(expected, bool) or expected is None:
        return got is expected
    if _is_number(expected):
        return _is_number(got) and float(got) == float(expected)
    if isinstance(expected, str):
        return isinstance(got, str) and got.strip().casefold() == expected.strip().casefold()
    if isinstance(expected, list):
        return (isinstance(got, list) and len(got) == len(expected)
                and all(value_matches(e, g) for e, g in zip(expected, got, strict=True)))
    if isinstance(expected, dict):
        return (isinstance(got, dict) and set(got) == set(expected)
                and all(value_matches(expected[k], got[k]) for k in expected))
    return False


def _argument_errors(expected_args: dict[str, Any], got_args: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for name, spec in expected_args.items():
        is_optional = _is_spec(spec) and "optional" in spec
        if name not in got_args:
            if not is_optional:
                errors.append(f"missing argument {name!r}")
            continue
        if not value_matches(spec, got_args[name]):
            errors.append(f"argument {name!r}: got {got_args[name]!r}")
    for name in got_args:
        if name not in expected_args:
            errors.append(f"unexpected argument {name!r}={got_args[name]!r}")
    return errors


def tool_schemas(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{tool name: parameters schema} from an OpenAI `tools` list."""
    schemas: dict[str, dict[str, Any]] = {}
    for tool in tools:
        function = tool.get("function", tool)
        schemas[function["name"]] = function.get("parameters") or {"type": "object", "properties": {}}
    return schemas


def call_errors(expected_call: dict[str, Any], actual_call: dict[str, Any],
                schemas: dict[str, dict[str, Any]]) -> list[str]:
    """Every reason `actual_call` fails `expected_call` (an empty list means it passes)."""
    name = actual_call.get("name")
    if actual_call.get("parse_error"):
        return [f"{name}: arguments did not parse ({actual_call['parse_error']})"]
    if name not in schemas:
        return [f"called unknown tool {name!r}"]
    if name != expected_call["name"]:
        return [f"called {name!r}, expected {expected_call['name']!r}"]
    arguments = actual_call.get("arguments")
    if not isinstance(arguments, dict):
        return [f"{name}: arguments are not an object"]
    errors = [f"{name}: {e}" for e in schema_errors(arguments, schemas[name])]
    errors += [f"{name}: {e}" for e in _argument_errors(expected_call.get("arguments", {}) or {}, arguments)]
    return errors


def match_tool_calls(expected: dict[str, Any], actual_calls: list[dict[str, Any]],
                     tools: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """(passed, reasons). Each actual call is {"name": str, "arguments": dict, "parse_error": str|None}."""
    expected_calls = expected.get("calls", []) or []
    schemas = tool_schemas(tools)
    if not expected_calls:
        if actual_calls:
            return False, [f"expected no tool call, got {[c.get('name') for c in actual_calls]}"]
        return True, []
    if len(actual_calls) != len(expected_calls):
        names = [c.get("name") for c in actual_calls]
        return False, [f"expected {len(expected_calls)} call(s), got {len(actual_calls)}: {names}"]
    if expected.get("order", "any") == "exact":
        errors = [e for exp, act in zip(expected_calls, actual_calls, strict=True)
                  for e in call_errors(exp, act, schemas)]
        return not errors, errors
    fewest: list[str] | None = None
    for permutation in itertools.permutations(actual_calls):
        errors = [e for exp, act in zip(expected_calls, permutation, strict=True)
                  for e in call_errors(exp, act, schemas)]
        if not errors:
            return True, []
        if fewest is None or len(errors) < len(fewest):
            fewest = errors
    return False, fewest or ["no pairing of the calls matched"]
