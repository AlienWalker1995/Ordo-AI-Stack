"""The orchestration MCP tools Hermes sees must all be able to work.

Read from source with `ast` (the `mcp` package is not a CI test dependency). After the v2 cutover
two tools could only fail: `assign_model_gpu` hits a route that answers 410 (GPU pins are set at
render time) and `register_model` a route the control plane never served. `set_active_model`
worked, but through the registry's .env write, which the next render silently undoes; it now goes
through the dashboard's catalog switch, the one path that survives a render.
"""
import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "services" / "orchestration" / "server.py"


def _tools() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    tools = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool" for d in node.decorator_list
        ):
            tools[node.name] = node
    return tools


def test_tools_that_can_only_fail_are_gone():
    tools = _tools()
    assert "assign_model_gpu" not in tools
    assert "register_model" not in tools


def test_set_active_model_uses_the_catalog_switch():
    source = ast.get_source_segment(SERVER.read_text(encoding="utf-8"), _tools()["set_active_model"])
    assert "/api/models/switch" in source
    assert "/enable" not in source


def test_the_catalog_is_listable_so_hermes_can_pick_an_id():
    source = ast.get_source_segment(SERVER.read_text(encoding="utf-8"), _tools()["list_model_catalog"])
    assert '"/api/models"' in source
