"""Typed workflow templates: JSON Schema validation + compile to API-format graph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .param_placeholders import apply_param_placeholders
from .workflow_boundary import assert_api_workflow

TEMPLATES_SUBDIR = "builtin_templates"


def _templates_dir() -> Path:
    return Path(__file__).resolve().parent / TEMPLATES_SUBDIR


def list_template_ids() -> list[str]:
    d = _templates_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def load_template(template_id: str) -> dict[str, Any]:
    root = _templates_dir()
    path = (root / f"{template_id}.json").resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as e:
        raise ValueError(f"Invalid template_id: {template_id}") from e
    if not path.is_file():
        raise FileNotFoundError(f"Unknown template: {template_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_params(params: dict[str, Any], schema: dict[str, Any]) -> None:
    Draft202012Validator(schema).validate(params)


def compile_template(
    template: dict[str, Any],
    params: dict[str, Any],
    *,
    workflows_dir: Path,
) -> dict[str, Any]:
    """Load workflow_file under workflows_dir, validate params, apply PARAM_* placeholders."""
    schema = template.get("parameter_schema")
    if isinstance(schema, dict):
        validate_params(params, schema)

    rel = (template.get("workflow_file") or "").strip()
    if not rel:
        raise ValueError("template missing workflow_file")

    wf_path = (workflows_dir / rel).resolve()
    root = workflows_dir.resolve()
    try:
        wf_path.relative_to(root)
    except ValueError as e:
        raise ValueError("workflow_file escapes workflows directory") from e

    if not wf_path.is_file():
        raise FileNotFoundError(f"Workflow file not found: {wf_path}")

    workflow = json.loads(wf_path.read_text(encoding="utf-8"))
    assert_api_workflow(workflow, context="workflow_file")
    return apply_param_placeholders(workflow, params)
