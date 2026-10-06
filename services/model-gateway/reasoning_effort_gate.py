"""LiteLLM custom callback: strip Anthropic `thinking` before the Qwen backend.

Wired in `litellm_config.yaml` as:
    litellm_settings:
      callbacks: ["reasoning_effort_gate.reasoning_effort_gate_instance"]

Why: Claude Code (and other Anthropic-API clients) send a `thinking` budget on
/v1/messages. For a non-Claude target model LiteLLM's Anthropic adapter translates
that budget into an OpenAI-style `reasoning_effort` ("high"/"medium"/"low"/
"minimal"). The Qwen3.6 chat template only accepts xhigh/medium/low, so "high"
(and "minimal") raise a Jinja `raise_exception` and 400 the whole request.

`additional_drop_params: [reasoning_effort]` does NOT fix it: that filter runs in
the OpenAI provider's get_optional_params, but the adapter re-injects
reasoning_effort into the request AFTER that filter, so the value survives to the
backend. The `async_pre_request_hook` runs earlier — inside the Anthropic
/v1/messages handler, before the thinking->reasoning_effort translation — and its
return value replaces the request kwargs. Nulling `thinking` there means the
adapter has nothing to translate, so no reasoning_effort is ever sent. The backend
then uses its own default reasoning effort.

This hook only fires on the Anthropic /v1/messages path, so OpenAI-compatible
clients (which send no `thinking` param) are unaffected.
"""
from __future__ import annotations

import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("reasoning_effort_gate")


class ReasoningEffortGate(CustomLogger):
    """LiteLLM CustomLogger: drops the Anthropic `thinking` param (and any
    pre-translated `reasoning_effort`) before the request reaches the Qwen
    backend, so the chat template never sees an effort value it rejects."""

    async def async_pre_request_hook(
        self, model: str, messages: list, kwargs: dict
    ) -> dict | None:
        if not isinstance(kwargs, dict):
            return None
        # Null out `thinking` so the adapter's thinking->reasoning_effort
        # translation has nothing to act on. The handler pops this key after the
        # hook, so None overrides the request's original thinking budget and the
        # adapter (which only translates a non-None thinking) sends no
        # reasoning_effort at all.
        kwargs["thinking"] = None
        # Belt and braces: drop a reasoning_effort that an earlier stage already
        # injected, in case a future LiteLLM version translates before the hook.
        kwargs.pop("reasoning_effort", None)
        return kwargs


# Module-level singleton, referenced from litellm_config.yaml (litellm_settings.callbacks).
reasoning_effort_gate_instance = ReasoningEffortGate()
