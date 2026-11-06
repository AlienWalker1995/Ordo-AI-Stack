"""Which `docker diff` entries count as a hand-edited image file (ordo/control/broker.py).

The agent's image declares /opt/hermes immutable; on 2026-11-06 the agent patched three upstream
modules there as root, and `docker diff` showed exactly those files plus the directories above them
and the bytecode the interpreter rewrote for them. Only the three source files are a change.
"""
from __future__ import annotations

from pathlib import Path

from ordo.control.broker import IMMUTABLE_PATHS_LABEL, changed_immutable_files, immutable_roots

ROOT = Path(__file__).resolve().parents[2]

OBSERVED_2026_11_06 = """C /opt/hermes
C /opt/hermes/agent
C /opt/hermes/agent/status_output.py
A /opt/hermes/agent/__pycache__
A /opt/hermes/agent/__pycache__/status_output.cpython-314.pyc
C /opt/hermes/hermes_cli
C /opt/hermes/hermes_cli/__pycache__
A /opt/hermes/hermes_cli/__pycache__/left_core_migration.cpython-314.pyc
C /opt/hermes/hermes_cli/left_core_migration.py
C /opt/hermes/gateway
C /opt/hermes/gateway/display_config.py
A /opt/hermes/gateway/__pycache__
A /opt/hermes/gateway/__pycache__/display_config.cpython-314.pyc
C /run/s6
A /run/s6/state
C /tmp
A /tmp/scratch.txt""".splitlines()


def test_the_observed_hotfix_counts_exactly_its_three_source_files():
    assert changed_immutable_files(OBSERVED_2026_11_06, ["/opt/hermes"]) == [
        "/opt/hermes/agent/status_output.py",
        "/opt/hermes/gateway/display_config.py",
        "/opt/hermes/hermes_cli/left_core_migration.py",
    ]


def test_runtime_writes_outside_the_roots_and_bytecode_alone_count_nothing():
    lines = ["C /opt/hermes", "C /opt/hermes/agent", "A /opt/hermes/agent/__pycache__",
             "A /opt/hermes/agent/__pycache__/x.cpython-314.pyc", "C /run/s6", "A /tmp/x"]
    assert changed_immutable_files(lines, ["/opt/hermes"]) == []


def test_an_added_or_deleted_file_counts_and_a_sibling_prefix_does_not():
    lines = ["A /opt/hermes/new_module.py", "D /opt/hermes/old_module.py", "A /opt/hermes-extra/x.py"]
    assert changed_immutable_files(lines, ["/opt/hermes"]) == ["/opt/hermes/new_module.py",
                                                                "/opt/hermes/old_module.py"]


def test_the_label_value_is_comma_separated_absolute_paths():
    assert immutable_roots("/opt/hermes, /opt/ordo/ ,relative,") == ["/opt/hermes", "/opt/ordo"]
    assert immutable_roots("") == []


def test_the_agent_image_declares_upstreams_installed_tree_immutable():
    dockerfile = (ROOT / "services" / "hermes" / "Dockerfile").read_text(encoding="utf-8")
    assert f'LABEL {IMMUTABLE_PATHS_LABEL}="/opt/hermes"' in dockerfile
