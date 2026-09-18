"""E11 (safety fix): the harness system prompts, kept in ordo_evals.prompts (a pure module, no
inspect_ai import) specifically so they can be exercised here without the eval-framework dependency
that suites/harness.py and suites/harness_domain.py pull in - see prompts.py's module docstring."""
from __future__ import annotations

from ordo_evals import prompts


def test_eval_system_prompt_forbids_gpu_work():
    assert "GPU" in prompts.EVAL_SYSTEM_PROMPT


def test_domain_system_prompt_layers_the_read_only_instruction_on_top_of_the_shared_prompt():
    """harness_domain sends real operator asks through Hermes (full tools, Docker socket, real repo
    access); this instruction is the second of two independent safety layers (the mutation label is
    the first - see private_dataset.py / harness_domain.run) telling Hermes to answer any item that
    does reach it by inspecting only."""
    assert prompts.DOMAIN_SYSTEM_PROMPT.startswith(prompts.EVAL_SYSTEM_PROMPT)
    assert prompts.DOMAIN_SYSTEM_PROMPT != prompts.EVAL_SYSTEM_PROMPT
    for phrase in ("inspecting state only", "do not commit", "do not push"):
        assert phrase in prompts.DOMAIN_SYSTEM_PROMPT.lower()
