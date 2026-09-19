"""E19 (round-7 fix): detecting that a trajectory read a PRIOR eval run's own sessions
(ordo_evals.replay) - pure, against the tool-result shapes a history search returns."""
from __future__ import annotations

import json

from ordo_evals import replay
from ordo_evals.hermes_turn import session_id_for

RUN = "loop4b-20260919-1048"
EARLIER = "loop3-20260918-1644"


def session_search_result(*session_ids):
    """What a history search hands back: rows naming the sessions it matched."""
    return json.dumps({"results": [{"session_id": sid, "snippet": "an earlier answer"} for sid in session_ids]})


def test_a_trajectory_that_read_a_prior_eval_session_is_replay_aware():
    prior = session_id_for(EARLIER, "harness_domain", "pd-968fd4c839b7")
    fields = replay.item_fields([json.dumps({"results": []}), session_search_result(prior)], RUN)
    assert fields["replay_aware"] is True
    assert fields["replay_prior_run_ids"] == [EARLIER]


def test_reading_this_runs_own_sessions_is_not_contamination():
    """An item is allowed to see itself: only ANOTHER run's sessions make a run non-independent."""
    own = session_id_for(RUN, "harness_domain", "pd-968fd4c839b7")
    fields = replay.item_fields([session_search_result(own)], RUN)
    assert fields["replay_aware"] is False and fields["replay_prior_run_ids"] == []


def test_reading_a_non_eval_session_is_not_replay():
    """Hermes's ordinary Discord/cron sessions are its own memory, not this harness replaying itself."""
    fields = replay.item_fields([session_search_result("discord-1548337027508998227-2026-09-18")], RUN)
    assert fields["replay_aware"] is False and fields["replay_prior_run_ids"] == []


def test_a_trajectory_that_read_nothing_is_not_replay_aware():
    assert replay.item_fields([], RUN) == {"replay_aware": False, "replay_prior_run_ids": []}
    assert replay.item_fields([None, json.dumps({"output": "ok", "exit_code": 0})], RUN) == {
        "replay_aware": False, "replay_prior_run_ids": []}


def test_a_skill_document_quoting_a_session_id_as_an_example_is_not_a_history_read():
    """The real loop4b false positive: one of Hermes's own skill documents explains the eval session
    naming convention and quotes a session id as an EXAMPLE. Five of the six items a naive
    "appears anywhere" test flagged were this one document, read through `skill_view`. Prose that
    mentions a session id is a mention, not a read."""
    doc = json.dumps({"success": True, "name": "hermes-eval-sessions", "content": (
        "## eval sessions\n\nThe session ID is opaque (e.g. "
        "`eval-loop4-20260919-0025-harness_ops-ops-06-vault-create`) - read it from the "
        "HERMES_SESSION_CHAT_ID env var.\n")})
    fields = replay.item_fields([doc], RUN)
    assert fields["replay_aware"] is False and fields["replay_prior_run_ids"] == []


def test_a_warning_naming_a_sibling_session_in_a_sentence_is_not_a_history_read():
    """The other real loop4b shape: a write_file result warning that a sibling agent had touched the
    same /tmp file, naming that agent's session mid-sentence. Real cross-run leakage of a different
    kind (shared scratch files), but not the history read this metric measures - and counting it here
    would blur two different effects into one number."""
    warning = json.dumps({"success": True, "_warning": (
        "/tmp/resolve_channels.py was modified by sibling subagent "
        "'eval-loop3-20260918-1644-harness_domain-pd-2e62f0ef467c' but this agent never read it.")})
    assert replay.item_fields([warning], RUN)["replay_aware"] is False


def test_a_session_id_nested_in_a_json_string_inside_a_result_still_counts():
    """A tool that returns its real payload as a JSON STRING (Hermes's terminal does) must not hide a
    genuine history read behind one level of encoding."""
    inner = json.dumps({"results": [{"session_id": session_id_for(EARLIER, "harness_honesty", "hon-01")}]})
    outer = json.dumps({"output": inner, "exit_code": 0, "error": None})
    assert replay.item_fields([outer], RUN)["replay_prior_run_ids"] == [EARLIER]


def test_several_prior_runs_are_all_named_once_each():
    ids = [session_id_for(EARLIER, "harness_honesty", "hon-01"),
           session_id_for(EARLIER, "harness_ops", "ops-01-terminal-product"),
           session_id_for("loop2-20260917-2000", "harness_domain", "pd-1a2b3c")]
    fields = replay.item_fields([session_search_result(*ids)], RUN)
    assert fields["replay_prior_run_ids"] == ["loop2-20260917-2000", EARLIER]


def test_a_run_id_containing_dashes_is_split_at_the_suite_name():
    """The run id itself carries dashes, so the pattern anchors on the suite name between the run id
    and the item id rather than guessing where one ends."""
    match = replay.EVAL_SESSION_ID.search(session_id_for(EARLIER, "harness_ops", "ops-07-vault-write-readback"))
    assert match is not None
    assert match.group("run") == EARLIER
    assert match.group("suite") == "harness_ops"
    assert match.group("item") == "ops-07-vault-write-readback"


def test_a_session_id_found_in_free_text_still_counts():
    """A terminal read of state.db prints session ids in a table, not JSON - the detection is by the
    session-id convention, never by which tool produced the result."""
    text = f"id                                       started_at\n{session_id_for(EARLIER, 'harness_ops', 'ops-03')}  1758..."
    fields = replay.item_fields([text], RUN)
    assert fields["replay_aware"] is True and fields["replay_prior_run_ids"] == [EARLIER]
