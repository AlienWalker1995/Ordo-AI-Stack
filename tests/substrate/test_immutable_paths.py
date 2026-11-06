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


BYTECODE_ONLY_HOTFIX = """C /opt/hermes
C /opt/hermes/agent
C /opt/hermes/agent/__pycache__
C /opt/hermes/agent/__pycache__/status_output.cpython-314.pyc""".splitlines()


def test_a_rewritten_bytecode_cache_with_an_untouched_source_counts():
    # The hotfix the all-pyc drop existed to hide: the agent rewrote only the .pyc, with a header
    # matching the untouched source's mtime, so the interpreter runs the new bytes while `docker
    # diff` shows no source change.
    assert changed_immutable_files(BYTECODE_ONLY_HOTFIX, ["/opt/hermes"]) == [
        "/opt/hermes/agent/__pycache__/status_output.cpython-314.pyc"]


FRESH_CONTAINER_BOOT = """C /opt/hermes
C /opt/hermes/plugins/ops-router
A /opt/hermes/plugins/ops-router/__pycache__
A /opt/hermes/plugins/ops-router/__pycache__/ops_client.cpython-314.pyc
C /run/s6
A /tmp/x""".splitlines()


def test_a_fresh_container_that_writes_caches_for_untouched_sources_reads_zero():
    # A recreated container compiles modules whose bytecode the image did not bake in (the
    # ops-router plugin, COPY'd without its __pycache__); those ADDED entries are the interpreter
    # doing its job on first import, not a hand edit, so a healthy recreate must not page.
    assert changed_immutable_files(FRESH_CONTAINER_BOOT, ["/opt/hermes"]) == []


def test_a_rewritten_cache_of_a_source_that_also_changed_counts_once():
    lines = ["C /opt/hermes", "C /opt/hermes/agent/status_output.py",
             "C /opt/hermes/agent/__pycache__",
             "C /opt/hermes/agent/__pycache__/status_output.cpython-314.pyc"]
    assert changed_immutable_files(lines, ["/opt/hermes"]) == ["/opt/hermes/agent/status_output.py"]


def test_the_label_value_is_comma_separated_absolute_paths():
    assert immutable_roots("/opt/hermes, /opt/ordo/ ,relative,") == ["/opt/hermes", "/opt/ordo"]
    assert immutable_roots("") == []


def test_the_agent_image_declares_upstreams_installed_tree_immutable():
    dockerfile = (ROOT / "services" / "hermes" / "Dockerfile").read_text(encoding="utf-8")
    assert f'LABEL {IMMUTABLE_PATHS_LABEL}="/opt/hermes"' in dockerfile
