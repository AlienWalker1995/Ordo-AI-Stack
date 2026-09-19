"""E19 (round-7 fix): measure how far an eval run can answer itself from the PREVIOUS eval run.

Every iteration replays the same private asks at the same agent, and that agent keeps its own
history. In loop4b-20260919-1048, `pd-968fd4c839b7` (does a deleted Discord conversation still sit in
Hermes's memory?) was answered correctly and then, accurately, with the observation that this was the
fourth time it had been asked that exact question - naming two earlier eval runs. Hermes had searched
its own session history, found this harness's earlier sessions, and could have answered from them
instead of doing the work. The contamination grows with every iteration, and the upcoming
system-prompt experiment compares two runs over identical datasets, so it lands squarely on the thing
being measured.

Hiding history from Hermes is not the fix (an agent that remembers is the product; a harness that
pretends otherwise measures a different agent). Measuring it is: per item, `replay_aware` records
whether any TOOL RESULT in the trajectory referenced an eval session id belonging to a DIFFERENT run,
and `replay_prior_run_ids` names those runs so a reader can see which earlier run leaked in.

Detection is by the session-id naming convention the harness itself creates
(`hermes_turn.session_id_for`: `eval-<run-id>-<suite>-<item-id>`), read out of tool results rather
than out of any one tool's name: Hermes has several ways to reach its own history (`session_search`,
a state.db read through the terminal, an MCP memory tool), and all of them have to return a session
id to be useful. A reference to THIS run's own sessions is not contamination - the item is allowed to
see itself - so only other run ids count.

**Where the id appears decides whether it counts**, and this is not a detail: on the first pass over
the real loop4b evidence, a naive "does a prior session id appear anywhere in a tool result" test
reported 6 of 29 harness items as replay-aware, and 5 of those 6 were one of Hermes's own skill
documents quoting `eval-loop4-20260919-0025-harness_ops-ops-06-vault-create` as an EXAMPLE of the
naming convention. Prose that mentions a session id is not a history read. So a match counts only
where a result is genuinely identifying a session:

  * as the value of a session-identifying FIELD of a structured result (`{"results": [{"session_id":
    "eval-..."}]}` - what `session_search` returns), including one nested inside a JSON string, or
  * at the START of a line of plain text, or right after a `session_id:` / `chat_id=` style label -
    the shape a terminal query of state.db prints.

Anything else (a session id inside a sentence, a document, a warning message) is a mention, not a
read. Conservative on purpose, the same way `stopping.py` is: an under-count leaves a real effect
slightly understated, while an over-count would manufacture contamination that is not there.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from typing import Any

from ordo_evals.suites import SUITE_ORDER

# eval-<run-id>-<suite>-<item-id>, the shape hermes_turn.session_id_for writes. The run-id group is
# lazy and the suite name is a fixed alternation from the registry, so a run id that itself contains
# dashes ("loop4b-20260919-1048") is split correctly. ids.safe_token has already reduced every part
# to [A-Za-z0-9_-] by the time a session id exists.
EVAL_SESSION_ID = re.compile(
    r"\beval-(?P<run>[A-Za-z0-9_-]+?)-(?P<suite>" + "|".join(SUITE_ORDER) + r")-(?P<item>[A-Za-z0-9_-]+)")

# Fields whose value IS a session identifier, in any tool's result shape.
SESSION_ID_FIELDS = frozenset({"session_id", "sessionid", "session", "parent_session_id",
                               "chat_id", "chatid", "id"})

# A plain-text line that identifies a session: the id starts the line (a table row), or follows a
# session-id label. Applied to the text immediately BEFORE a match, within its own line.
_LINE_PREFIX_IS_IDENTIFYING = re.compile(
    r"(?:^[\s\"'|,\[]*$"                                     # the id starts the line
    r"|(?:session[_ -]?id|chat[_ -]?id|session)\s*[:=]\s*[\"'`]?$)",  # ... or follows a label
    re.IGNORECASE)


def null_fields() -> dict[str, Any]:
    """The replay fields for an item whose trajectory could not be read: nulls, never guesses (see
    `trajectory.behaviour_known`, which is what separates these from a real "read nothing prior")."""
    return {"replay_aware": None, "replay_prior_run_ids": None}


def _parse(text: str) -> Any:
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None


def _identified_sessions(payload: Any) -> Iterator[str]:
    """Every value of a session-identifying field anywhere in a parsed result (JSON strings nested
    inside it are parsed one level at a time and walked too)."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, str):
                if str(key).lower() in SESSION_ID_FIELDS:
                    yield value
                else:
                    yield from _identified_sessions(_parse(value))
            else:
                yield from _identified_sessions(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _identified_sessions(value)


def _text_identified_sessions(content: str) -> Iterator[str]:
    """Session ids that a plain-text result is IDENTIFYING rather than mentioning (see the docstring)."""
    for match in EVAL_SESSION_ID.finditer(content):
        line_start = content.rfind("\n", 0, match.start()) + 1
        if _LINE_PREFIX_IS_IDENTIFYING.search(content[line_start:match.start()]):
            yield match.group(0)


def prior_run_ids(tool_results: Sequence[str | None], run_id: str) -> list[str]:
    """Every OTHER run's id whose session a tool result actually identified, sorted."""
    found: set[str] = set()
    for content in tool_results:
        if not content:
            continue
        candidates = list(_identified_sessions(_parse(content))) + list(_text_identified_sessions(content))
        for candidate in candidates:
            match = EVAL_SESSION_ID.match(candidate.strip())
            if match and match.group("run") != run_id:
                found.add(match.group("run"))
    return sorted(found)


def item_fields(tool_results: Sequence[str | None], run_id: str) -> dict[str, Any]:
    """`replay_aware` / `replay_prior_run_ids` for one item, from its tool results."""
    prior = prior_run_ids(tool_results, run_id)
    return {"replay_aware": bool(prior), "replay_prior_run_ids": prior}
