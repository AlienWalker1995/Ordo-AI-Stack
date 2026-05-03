"""Tests for the ComfyUI guardian's multi-target support.

The guardian historically managed a single in-stack service (`llamacpp`).
This file exercises the multi-target extension that lets it pause arbitrary
combinations of in-stack services AND cross-project containers (e.g. a
sibling ai-toolkit container) before a ComfyUI workflow runs.

Test surface:
  - Parsing of `COMFYUI_GUARDIAN_TARGET` from env into a list
  - Container resolution that falls back from compose-service-id to
    literal container name
  - Status dict exposes both the legacy `target` (string) and the new
    `targets` (list) fields
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def reload_main(monkeypatch):
    """Reload ops_controller.main after setting env vars so module-level
    constants pick up the test values. Returns the freshly-imported module.
    """
    from conftest import _install_docker_stub
    _install_docker_stub()
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token-for-test")

    def _go():
        import ops_controller.main as m
        importlib.reload(m)
        return m

    return _go


def test_target_list_default_is_single_llamacpp(reload_main):
    """No env var set → list with the single legacy default."""
    m = reload_main()
    assert m._guardian_target_list() == ["llamacpp"]


def test_target_list_single_value_unchanged(monkeypatch, reload_main):
    """Single string (legacy form) parses to a single-element list."""
    monkeypatch.setenv("COMFYUI_GUARDIAN_TARGET", "llamacpp")
    m = reload_main()
    assert m._guardian_target_list() == ["llamacpp"]


def test_target_list_multi_value_comma_separated(monkeypatch, reload_main):
    """The multi-target use case: in-stack services + cross-project containers."""
    monkeypatch.setenv(
        "COMFYUI_GUARDIAN_TARGET",
        "llamacpp,llamacpp-embed,ai-toolkit-ai-toolkit-1",
    )
    m = reload_main()
    assert m._guardian_target_list() == [
        "llamacpp",
        "llamacpp-embed",
        "ai-toolkit-ai-toolkit-1",
    ]


def test_target_list_strips_whitespace(monkeypatch, reload_main):
    """Cosmetic whitespace around entries shouldn't change semantics."""
    monkeypatch.setenv("COMFYUI_GUARDIAN_TARGET", "llamacpp ,  llamacpp-embed , ai-toolkit-ai-toolkit-1")
    m = reload_main()
    assert m._guardian_target_list() == [
        "llamacpp",
        "llamacpp-embed",
        "ai-toolkit-ai-toolkit-1",
    ]


def test_target_list_skips_empty_entries(monkeypatch, reload_main):
    """Trailing or doubled commas don't introduce empty targets."""
    monkeypatch.setenv("COMFYUI_GUARDIAN_TARGET", "llamacpp,,llamacpp-embed,")
    m = reload_main()
    assert m._guardian_target_list() == ["llamacpp", "llamacpp-embed"]


def test_target_list_empty_falls_back_to_default(monkeypatch, reload_main):
    """Pathological all-blank value falls back to the historic default —
    silent guardian disablement would be surprising; we'd rather pause
    llamacpp by default than nothing."""
    monkeypatch.setenv("COMFYUI_GUARDIAN_TARGET", "  , , ")
    m = reload_main()
    assert m._guardian_target_list() == ["llamacpp"]


def test_status_exposes_targets_list(monkeypatch, reload_main):
    """Status dict gains a `targets` field; legacy `target` (the raw string) stays
    for backwards-compat with anything reading the old shape."""
    monkeypatch.setenv("COMFYUI_GUARDIAN_TARGET", "llamacpp,ai-toolkit-ai-toolkit-1")
    m = reload_main()
    assert m._guardian_status["target"] == "llamacpp,ai-toolkit-ai-toolkit-1"
    assert m._guardian_status["targets"] == ["llamacpp", "ai-toolkit-ai-toolkit-1"]
    # New per-target tracking field starts empty:
    assert m._guardian_status["paused_targets"] == []


def test_resolve_in_stack_service_returns_matching_containers(reload_main, monkeypatch):
    """When the target names an in-stack compose service, return all containers
    Docker reports for that label — and skip the container-name fallback."""
    from unittest.mock import MagicMock
    m = reload_main()
    sentinel = MagicMock()
    sentinel.status = "running"
    monkeypatch.setattr(m, "_containers_for_service", lambda name: [sentinel] if name == "llamacpp" else [])

    containers = m._resolve_guardian_containers("llamacpp")
    assert containers == [sentinel]


def test_resolve_falls_back_to_container_name(reload_main, monkeypatch):
    """If no in-stack service matches, look up the literal container name in
    the daemon (handles cross-project containers like `ai-toolkit-*`)."""
    from unittest.mock import MagicMock
    m = reload_main()
    monkeypatch.setattr(m, "_containers_for_service", lambda name: [])

    sentinel = MagicMock()
    sentinel.status = "running"

    def _get_returns_sentinel(name):
        assert name == "ai-toolkit-ai-toolkit-1"
        return sentinel

    client = m._docker_client()
    client.containers.get.side_effect = _get_returns_sentinel

    containers = m._resolve_guardian_containers("ai-toolkit-ai-toolkit-1")
    assert containers == [sentinel]


def test_resolve_unknown_target_returns_empty(reload_main, monkeypatch):
    """Unknown target → empty list (guardian skips silently for that cycle)."""
    import docker
    m = reload_main()
    monkeypatch.setattr(m, "_containers_for_service", lambda name: [])

    def _raise(_name):
        raise docker.errors.NotFound("no such container")

    client = m._docker_client()
    client.containers.get.side_effect = _raise

    assert m._resolve_guardian_containers("nope-not-a-real-thing") == []
