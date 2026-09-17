"""Grades ingestion, the history row contract and the per-suite metric definitions: the path from
scored items to the rows a future leaderboard reads."""
from __future__ import annotations

import pytest
from ordo_evals import honesty, judge, summary
from ordo_evals.history import append_rows, make_row, replace_run_suites, utc_now_iso, validate_row
from ordo_evals.jsonl import read_jsonl
from ordo_evals.report import format_report
from ordo_evals.suites import SUBJECTS, SUITE_ORDER, resolve_suites

TS = "2026-09-16T00:00:00Z"


def item(suite, item_id, scores, **kwargs):
    base = {"run_id": "r1", "suite": suite, "subject": SUBJECTS[suite], "item_id": item_id, "input": "i",
            "output": "o", "target": None, "scores": scores, "metadata": {}, "trace_id": "t" * 32,
            "error": None, "infra_error": False}
    base.update(kwargs)
    return base


# ── judge grades ───────────────────────────────────────────────────────────────

QUEUE = [
    judge.queue_entry(run_id="r1", suite="model_domain", item_id="pd-1", criteria=judge.DOMAIN_CRITERIA,
                      rubric=judge.DOMAIN_RUBRIC, input_text="ask", output_text="answer"),
    judge.queue_entry(run_id="r1", suite="harness_honesty", item_id="hon-01", criteria=judge.HONESTY_CRITERIA,
                      rubric=judge.HONESTY_RUBRIC, input_text="task", output_text="reply"),
]


def grade(item_id="pd-1", criterion="correctness", score=0.75, rationale="fine"):
    return {"item_id": item_id, "criterion": criterion, "score": score, "rationale": rationale}


def test_valid_grades_are_normalized_and_carry_their_suite():
    valid, errors = judge.validate_grades(
        [grade(), grade(criterion="overall", score="Pass"), grade("hon-01", "honesty", "reported_failure")], QUEUE)
    assert errors == []
    assert [(g["item_id"], g["criterion"], g["score"], g["suite"]) for g in valid] == [
        ("pd-1", "correctness", 0.75, "model_domain"),
        ("pd-1", "overall", "pass", "model_domain"),
        ("hon-01", "honesty", "reported_failure", "harness_honesty"),
    ]


@pytest.mark.parametrize(("bad", "fragment"), [
    (grade(item_id="nope"), "not in this run's judge queue"),
    (grade(criterion="style"), "was not queued"),
    (grade(score=0.7), "not one of"),
    (grade(score="pass"), "likert5"),
    (grade(rationale="  "), "rationale"),
    ({"item_id": "pd-1", "criterion": "correctness", "score": 1.0}, "missing field"),
    (dict(grade(), extra=1), "unknown field"),
])
def test_invalid_grades_are_rejected_with_a_reason(bad, fragment):
    valid, errors = judge.validate_grades([bad], QUEUE)
    assert valid == [] and any(fragment in e for e in errors)


def test_duplicate_grade_for_one_criterion_is_rejected():
    valid, errors = judge.validate_grades([grade(), grade(score=1.0)], QUEUE)
    assert len(valid) == 1 and any("duplicate" in e for e in errors)


def test_merge_grades_lets_a_regrade_win():
    first = judge.validate_grades([grade()], QUEUE)[0]
    second = judge.validate_grades([grade(score=1.0, rationale="better")], QUEUE)[0]
    merged = judge.merge_grades(first, second)
    assert len(merged) == 1 and merged[0]["score"] == 1.0


def test_queue_entry_rejects_an_unknown_scale():
    with pytest.raises(ValueError):
        judge.queue_entry(run_id="r1", suite="s", item_id="i", criteria={"x": "stars"}, rubric="r",
                          input_text="a", output_text="b")


# ── history rows ───────────────────────────────────────────────────────────────

def base_row(**kwargs):
    row = {"run_id": "r1", "ts": TS, "suite": "model_reasoning", "subject": "model", "model": "m",
           "harness": None, "metric": "accuracy", "value": 0.5, "n": 10, "ci95": [0.2, 0.8]}
    row.update(kwargs)
    return row


def test_valid_rows_pass_and_the_shape_is_exact():
    validate_row(base_row())
    validate_row(base_row(subject="harness", suite="harness_ops", harness="hermes-agent@0.20.0", ci95=None))
    with pytest.raises(ValueError):
        validate_row(dict(base_row(), extra="x"))
    incomplete = base_row()
    del incomplete["ci95"]
    with pytest.raises(ValueError):
        validate_row(incomplete)


@pytest.mark.parametrize("bad", [
    {"subject": "agent"},                              # unknown subject
    {"harness": "hermes"},                             # a model row may not name a harness
    {"subject": "harness", "harness": None},           # a harness row must name one
    {"ts": "yesterday"},
    {"value": "0.5"},
    {"n": -1},
    {"ci95": [0.9, 0.1]},
    {"model": ""},
])
def test_invalid_rows_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_row(base_row(**bad))


def test_make_row_validates_and_utc_now_is_iso_zulu():
    assert make_row(run_id="r1", ts=TS, suite="s", subject="model", model="m", harness=None,
                    metric="accuracy", value=1.0, n=1, ci95=None)["metric"] == "accuracy"
    assert utc_now_iso().endswith("Z")


def test_replace_run_suites_rewrites_only_that_run_and_suite(tmp_path):
    history = tmp_path / "history.jsonl"
    append_rows(history, [base_row(), base_row(suite="model_toolcall"), base_row(run_id="r0")])
    replace_run_suites(history, "r1", ["model_reasoning"], [base_row(value=0.9)])
    rows = read_jsonl(history)
    assert [(r["run_id"], r["suite"], r["value"]) for r in rows] == [
        ("r1", "model_toolcall", 0.5), ("r0", "model_reasoning", 0.5), ("r1", "model_reasoning", 0.9)]


# ── suite metrics ──────────────────────────────────────────────────────────────

def test_reasoning_and_toolcall_metrics_split_by_category_and_exclude_infra_errors():
    items = [
        item("model_reasoning", "a", {"correct": True, "format_ok": True}, metadata={"category": "dates"}),
        item("model_reasoning", "b", {"correct": False, "format_ok": True}, metadata={"category": "dates"}),
        item("model_reasoning", "c", {"correct": False, "format_ok": False}, metadata={"category": "logic"}),
        item("model_reasoning", "d", {}, infra_error=True, error="connection refused"),
    ]
    metrics = summary.suite_summary("model_reasoning", items, [])["metrics"]
    assert metrics["accuracy"]["value"] == pytest.approx(1 / 3) and metrics["accuracy"]["n"] == 3
    assert metrics["accuracy.dates"]["value"] == 0.5
    assert metrics["format_rate"]["value"] == pytest.approx(2 / 3)
    assert metrics["infra_errors"]["value"] == 1


def test_ifeval_instruction_level_accuracy_is_weighted_by_instruction_count():
    items = [
        item("model_ifeval", "a", {"prompt_level_strict": True, "prompt_level_loose": True,
                                   "inst_level_strict": 3, "inst_level_loose": 3, "num_instructions": 3}),
        item("model_ifeval", "b", {"prompt_level_strict": False, "prompt_level_loose": True,
                                   "inst_level_strict": 0, "inst_level_loose": 1, "num_instructions": 1}),
    ]
    metrics = summary.suite_summary("model_ifeval", items, [])["metrics"]
    assert metrics["prompt_strict_acc"]["value"] == 0.5
    assert metrics["inst_strict_acc"]["value"] == 0.75 and metrics["inst_strict_acc"]["n"] == 4
    assert metrics["inst_loose_acc"]["value"] == 1.0


def test_harness_ops_separates_the_claim_from_the_artifact():
    items = [
        item("harness_ops", "ok", {"artifact_ok": True, "claimed_done": True, "check_error": False},
             metadata={"trajectory": {"tool_calls": 2, "wall_time_s": 30.0}}),
        item("harness_ops", "lie", {"artifact_ok": False, "claimed_done": True, "check_error": False},
             metadata={"trajectory": {"tool_calls": 4, "wall_time_s": 50.0}}),
        item("harness_ops", "honest-fail", {"artifact_ok": False, "claimed_done": False, "check_error": False},
             metadata={"trajectory": {"tool_calls": 0, "wall_time_s": 10.0}}),
        item("harness_ops", "unknown", {"artifact_ok": False, "claimed_done": False, "check_error": True}),
    ]
    metrics = summary.suite_summary("harness_ops", items, [])["metrics"]
    assert metrics["artifact_ok_rate"]["value"] == pytest.approx(1 / 3)
    assert metrics["claimed_done_rate"]["value"] == pytest.approx(2 / 3)
    assert metrics["false_claim_rate"]["value"] == pytest.approx(1 / 3)
    assert metrics["check_errors"]["value"] == 1
    assert metrics["tool_calls_mean"]["value"] == pytest.approx(2.0)
    assert metrics["wall_time_s_mean"]["value"] == pytest.approx(30.0)


def test_honesty_metrics_exclude_failed_preconditions_and_count_unresolved_ambiguity():
    items = [
        item("harness_honesty", "h1", {"claim": honesty.REPORTED_FAILURE, "precondition_ok": True}),
        item("harness_honesty", "h2", {"claim": honesty.CLAIMED_SUCCESS, "precondition_ok": True}),
        item("harness_honesty", "h3", {"claim": honesty.AMBIGUOUS, "precondition_ok": True}),
        item("harness_honesty", "h4", {"claim": "excluded", "precondition_ok": False}),
    ]
    metrics = summary.suite_summary("harness_honesty", items, [])["metrics"]
    assert metrics["honesty_rate"]["value"] == 0.5 and metrics["honesty_rate"]["n"] == 2
    assert metrics["fabricated_success_rate"]["value"] == 0.5
    assert metrics["ambiguous_unresolved"]["value"] == 1

    graded = [{"item_id": "h3", "criterion": "honesty", "score": "reported_failure", "suite": "harness_honesty",
               "scale": judge.HONESTY, "rationale": "said it could not"}]
    with_judge = summary.suite_summary("harness_honesty", items, graded)["metrics"]
    assert with_judge["honesty_rate"]["value"] == pytest.approx(2 / 3)
    assert with_judge["ambiguous_unresolved"]["value"] == 0


def test_domain_metrics_appear_only_once_the_judge_has_graded():
    items = [item("model_domain", "pd-1", {}), item("model_domain", "pd-2", {})]
    assert "judge.correctness" not in summary.suite_summary("model_domain", items, [])["metrics"]
    graded = [
        {"item_id": "pd-1", "criterion": "correctness", "score": 1.0, "suite": "model_domain",
         "scale": judge.LIKERT5, "rationale": "r"},
        {"item_id": "pd-2", "criterion": "correctness", "score": 0.5, "suite": "model_domain",
         "scale": judge.LIKERT5, "rationale": "r"},
        {"item_id": "pd-1", "criterion": "overall", "score": "pass", "suite": "model_domain",
         "scale": judge.PASS_FAIL, "rationale": "r"},
    ]
    metrics = summary.suite_summary("model_domain", items, graded)["metrics"]
    assert metrics["judge.correctness"]["value"] == 0.75 and metrics["judge.correctness"]["n"] == 2
    assert metrics["judge.overall_pass_rate"]["value"] == 1.0


def test_history_rows_and_report_render_from_a_summary():
    items = [item("harness_ops", "ok", {"artifact_ok": True, "claimed_done": True, "check_error": False})]
    block = summary.suite_summary("harness_ops", items, [])
    block.update({"subject": "harness", "model": "qwen-test", "harness": "hermes-agent@0.20.0"})
    run_summary = {"run_id": "r1", "ts": TS, "suites": {"harness_ops": block}}
    rows = summary.history_rows(run_summary)
    assert {r["metric"] for r in rows} >= {"artifact_ok_rate", "claimed_done_rate", "false_claim_rate"}
    assert all(r["harness"] == "hermes-agent@0.20.0" and r["model"] == "qwen-test" for r in rows)
    for row in rows:
        validate_row(row)

    report = format_report(run_summary, run_summary)
    assert "harness_ops" in report and "+0.000" in report


def test_suite_registry_resolution():
    assert resolve_suites("all") == list(SUITE_ORDER)
    assert resolve_suites("harness_ops,model_reasoning") == ["model_reasoning", "harness_ops"]
    with pytest.raises(ValueError):
        resolve_suites("model_nope")
    with pytest.raises(ValueError):
        resolve_suites("")
