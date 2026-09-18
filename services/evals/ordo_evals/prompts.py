"""Pure system-prompt text for the harness suites (Hermes API-server turns).

Kept separate from suites/harness.py and suites/harness_domain.py, which both import inspect_ai
(services/evals/requirements.txt's eval-framework dependency - available inside the evals container,
not in the root test environment; see tests/requirements.txt and .github/workflows/ci.yml's `pytest`
job), so these constants can be exercised directly in tests/evals without pulling that dependency in,
the same way honesty.py's HARNESS_REPORTING_PROTOCOL and checks.py's pure logic already are.
"""
from __future__ import annotations

# Layered on top of Hermes's own system prompt (the API server treats a system message as an
# ephemeral addition). It exists for SAFETY, not to steer answers: an eval must never start GPU work
# (the scheduler-lease rule) or wander outside the paths a task names. Used by every harness suite.
EVAL_SYSTEM_PROMPT = (
    "This conversation is an automated evaluation of the Ordo stack. Do not start GPU work: no image, "
    "video or audio generation and no ComfyUI workflows. Do not modify anything outside the paths the "
    "task names.")

# E11 (safety fix): harness_domain sends real operator asks from the private candidate pool through
# Hermes, which has full tools, the Docker socket and real repo access. Iteration 2 hit exactly this:
# an agent_standalone item ("add hackernews to the ai-daily-news site") made Hermes clone a real repo,
# edit it, commit, and attempt to push - the push only failed because the GitHub tokens were expired.
# The mutation label (judge.PRIVATE_MUTATION / private_dataset.py) is the primary defence: a
# candidate a judge marked (or left unmarked) mutating never becomes a Sample in harness_domain.run()
# at all. This instruction is the second, independent layer, added to every turn that DOES reach
# Hermes through that suite. It is defence in depth, not a guarantee - an agent with full tool access
# can still choose to act against an instruction (see the README's harness_domain section).
DOMAIN_SAFETY_INSTRUCTION = (
    " This particular task is answerable by inspecting state only: reading files, running read-only "
    "lookups (search, git log/diff/show, docker ps/logs, cat/ls, a status or GET endpoint), and "
    "similar. Do not create, edit, or delete any file or note, do not run a command that has side "
    "effects, do not commit, and do not push or send anything to any remote. If answering this fully "
    "would require changing something, say so instead of doing it.")

DOMAIN_SYSTEM_PROMPT = EVAL_SYSTEM_PROMPT + DOMAIN_SAFETY_INSTRUCTION
