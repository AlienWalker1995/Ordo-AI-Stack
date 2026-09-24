"""First-run install defects found by a fresh-container onboarding run (2026-09-24).

1. `ordo init` printed `ordo render --source ...`, but --source was only a global flag, so the
   printed next step failed with "unrecognized arguments".
2. The package imports pydantic but did not declare it, so `pip install .` gave a CLI that
   crashed on start.
3. install.sh ran a plain `pip install .`, which copies ordo/ into site-packages. Every repo path
   the CLI derives from its own location (services/, catalog/, the BASE_PATH `ordo init` records)
   then pointed into the virtualenv. The CLI operates on a repo checkout, so it installs editable.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

from ordo import cli

ROOT = Path(__file__).resolve().parents[2]

SOURCE = """\
hardware: {gpus: [], ram_gb: 32, cpu_cores: 8, platform: Linux}
model: auto
tier: auto
plugins: []
site:
  BASE_PATH: /srv/ordo
  DATA_PATH: /srv/ordo/data
"""


def test_source_is_accepted_after_the_subcommand(tmp_path):
    src = tmp_path / "ordo.yaml"
    src.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"
    assert cli.main(["render", "--source", str(src), "--out", str(out)]) == 0
    assert "BASE_PATH=/srv/ordo" in (out / ".env").read_text(encoding="utf-8")


def test_a_global_source_still_works(tmp_path):
    src = tmp_path / "ordo.yaml"
    src.write_text(SOURCE, encoding="utf-8")
    assert cli.main(["--source", str(src), "render", "--out", str(tmp_path / "out")]) == 0


def _third_party_imports() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "ordo").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top != "ordo" and top not in sys.stdlib_module_names:
                    found.add(top)
    return found


def _declared() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        deps += extra
    return {dep.split("=")[0].split(">")[0].split("<")[0].split("[")[0].strip().lower() for dep in deps}


def test_every_third_party_import_is_a_declared_dependency():
    import_to_distribution = {"yaml": "pyyaml"}
    missing = {name for name in _third_party_imports()
               if import_to_distribution.get(name, name).lower() not in _declared()}
    assert missing == set()


def test_the_cli_starts_with_only_the_core_dependency():
    # The render core is PyYAML-only by design; the control-plane libraries are the `serve` extra
    # and must not be imported until `ordo serve` runs.
    blocked = ["pydantic", "fastapi", "uvicorn", "httpx"]
    lines = ["import sys"] + [f"sys.modules[{name!r}] = None" for name in blocked] + ["import ordo.cli"]
    code = "\n".join(lines)
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_script_installs_the_cli_editable():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "pip install --quiet -e ." in script
    assert "pip install --quiet ." not in script
