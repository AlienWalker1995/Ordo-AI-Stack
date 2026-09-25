"""The `ordo` package's layers import in one direction, and no two modules import each other.

`ordo/` is three layers (see ordo/__init__.py):

- `ordo.render`: the render engine and the contracts of what it renders. It imports no other layer,
  so ops-controller and the host render with the same code and nothing above can leak into a render.
- `ordo.control`: the control plane inside ops-controller. It imports `render` only.
- `ordo.host`: the operator's host commands. It imports `render` only; it reaches the control plane
  over HTTP (ops-controller's /status), never by import.

`ordo/cli.py` (with `__main__` and the package `__init__`) is the entry point and may import any layer;
`ordo/secret_env.py` is a stdlib-only leaf any layer may import.

The scan reads every import the interpreter can execute, function-local ones included: a local import
used to hide a cycle (images <-> render, compose <-> render, fetch <-> preflight, bringup <-> images).
Imports under `if TYPE_CHECKING:` never run and are skipped.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "ordo"

ENTRY_MODULES = {"ordo", "ordo.__main__", "ordo.cli"}
LEAF_MODULES = {"ordo.secret_env"}
# layer -> the layers its modules may import
ALLOWED = {
    "render": {"render", "leaf"},
    "control": {"render", "control", "leaf"},
    "host": {"render", "host", "leaf"},
    "leaf": set(),
}


def module_name(path: Path, package_dir: Path) -> str:
    parts = list(path.relative_to(package_dir.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def layer_of(module: str) -> str:
    if module in ENTRY_MODULES:
        return "entry"
    if module in LEAF_MODULES:
        return "leaf"
    parts = module.split(".")
    if len(parts) >= 2 and parts[1] in ("render", "control", "host"):
        return parts[1]
    return "unassigned"


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _runtime_imports(tree: ast.AST):
    """Every Import / ImportFrom node that can execute (anything outside `if TYPE_CHECKING:`)."""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking_guard(child):
                stack.extend(child.orelse)
                continue
            if isinstance(child, ast.Import | ast.ImportFrom):
                yield child
            stack.append(child)


def imported_modules(path: Path, package_dir: Path, known: set[str]) -> list[tuple[int, str]]:
    """(line, module) for each package module `path` imports at runtime."""
    this = module_name(path, package_dir)
    is_package = path.name == "__init__.py"
    base_parts = this.split(".") if is_package else this.split(".")[:-1]
    found = []
    for node in _runtime_imports(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        else:
            if node.level:
                anchor = base_parts[:len(base_parts) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            # `from pkg import name`: the name is a submodule when one exists, else an attribute of pkg
            targets = [f"{base}.{alias.name}" if f"{base}.{alias.name}" in known else base
                       for alias in node.names]
        for target in targets:
            if target in known:
                found.append((node.lineno, target))
    return found


def import_graph(package_dir: Path) -> dict[str, list[tuple[int, str]]]:
    files = sorted(package_dir.rglob("*.py"))
    known = {module_name(p, package_dir) for p in files}
    return {module_name(p, package_dir): imported_modules(p, package_dir, known) for p in files}


def direction_violations(package_dir: Path) -> list[str]:
    graph = import_graph(package_dir)
    problems = []
    for module, imports in sorted(graph.items()):
        layer = layer_of(module)
        if layer == "unassigned":
            problems.append(f"{module}: in no layer (move it into render/, control/ or host/)")
            continue
        if layer == "entry":
            continue
        for line, target in imports:
            target_layer = layer_of(target)
            if target_layer not in ALLOWED[layer]:
                problems.append(f"{module}:{line} imports {target} ({layer} -> {target_layer})")
    return problems


def import_cycles(package_dir: Path) -> list[list[str]]:
    """Every strongly connected group of more than one module (Tarjan), plus self-imports."""
    graph = {module: sorted({t for _line, t in imports}) for module, imports in import_graph(package_dir).items()}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    cycles: list[list[str]] = []
    counter = [0]

    def visit(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for succ in graph.get(node, []):
            if succ not in index:
                visit(succ)
                low[node] = min(low[node], low[succ])
            elif succ in on_stack:
                low[node] = min(low[node], index[succ])
        if low[node] == index[node]:
            group = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                group.append(member)
                if member == node:
                    break
            if len(group) > 1 or node in graph.get(node, []):
                cycles.append(sorted(group))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return sorted(cycles)


# --- the real package ---


def test_every_import_follows_the_layer_direction():
    assert direction_violations(PACKAGE) == []


def test_no_import_cycles_between_modules():
    assert import_cycles(PACKAGE) == []


def test_every_module_is_in_a_layer():
    graph = import_graph(PACKAGE)
    assert {layer_of(module) for module in graph} <= {"entry", "leaf", "render", "control", "host"}
    assert any(layer_of(m) == "control" for m in graph) and any(layer_of(m) == "host" for m in graph)


def test_serve_loads_no_host_module():
    """`ordo serve` (ops-controller's entrypoint) must not pull the host tooling into the control plane:
    cli.py imports a handler's module only when its command runs."""
    probe = ("import sys\n"
             "import ordo.cli, ordo.control.serve\n"
             "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('ordo.host'))))\n")
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


# --- the checker itself catches what it guards against ---


def _package(tmp_path: Path, files: dict[str, str]) -> Path:
    package_dir = tmp_path / "ordo"
    for rel, text in {"__init__.py": "", "render/__init__.py": "", "control/__init__.py": "",
                      "host/__init__.py": "", **files}.items():
        path = package_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return package_dir


def test_checker_reports_an_upward_import_even_inside_a_function(tmp_path):
    package_dir = _package(tmp_path, {
        "render/engine.py": "def f():\n    from ..host import images\n",
        "host/images.py": "",
        "control/api.py": "from ..host.images import x\n",
    })
    assert direction_violations(package_dir) == [
        "ordo.control.api:1 imports ordo.host.images (control -> host)",
        "ordo.render.engine:2 imports ordo.host.images (render -> host)",
    ]


def test_checker_skips_type_checking_imports(tmp_path):
    package_dir = _package(tmp_path, {
        "render/engine.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..host import images\n",
        "host/images.py": "",
    })
    assert direction_violations(package_dir) == []


def test_checker_reports_a_cycle_hidden_behind_a_local_import(tmp_path):
    package_dir = _package(tmp_path, {
        "render/compose.py": "def f():\n    from .engine import KEYS\n",
        "render/engine.py": "from . import compose\nKEYS = ()\n",
    })
    assert import_cycles(package_dir) == [["ordo.render.compose", "ordo.render.engine"]]


def test_checker_reports_a_module_outside_every_layer(tmp_path):
    package_dir = _package(tmp_path, {"stray.py": ""})
    assert direction_violations(package_dir) == ["ordo.stray: in no layer (move it into render/, control/ or host/)"]
