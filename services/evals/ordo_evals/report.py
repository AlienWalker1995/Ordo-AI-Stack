"""Compact text report of a run summary, optionally against a second run (deltas = run - compare)."""
from __future__ import annotations

from typing import Any


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _fmt_ci(ci: list[float] | None) -> str:
    return "-" if not ci else f"[{ci[0]:.3f}, {ci[1]:.3f}]"


def _contention_line(summary: dict[str, Any]) -> str | None:
    """One line for the run-level contention block, or None when the run has none recorded.

    The abandoned-work half (E23, round-10 fix) is what says how much of the run was spent on items
    the harness had already given up on: an overrun that is a large share of the run means the
    surviving items were measured while an abandoned item was still generating on the same slot, and
    a wait that timed out means it definitely was."""
    block = summary.get("contention")
    if not isinstance(block, dict):
        return None
    parts = [f"tokens/s median {_fmt(block.get('tokens_per_second_median'))}",
             f"min {_fmt(block.get('tokens_per_second_min'))}",
             f"over {block.get('n', 0)} item(s)",
             f"{block.get('slow_items', 0)} slow"]
    if block.get("abandoned_items"):
        parts.append(f"abandoned work {_fmt(block.get('abandoned_overrun_s'))}s over "
                     f"{block['abandoned_items']} item(s)")
        if block.get("abandoned_wait_timeouts"):
            parts.append(f"{block['abandoned_wait_timeouts']} wait(s) TIMED OUT (next item contended)")
    return "contention: " + ", ".join(parts)


def format_report(summary: dict[str, Any], compare: dict[str, Any] | None = None) -> str:
    header = ["suite", "metric", "value", "n", "ci95"]
    if compare is not None:
        header += [f"vs {compare['run_id']}", "delta"]
    rows: list[list[str]] = []
    for suite, block in sorted(summary["suites"].items()):
        other_metrics = ((compare or {}).get("suites", {}).get(suite) or {}).get("metrics", {})
        for metric, entry in sorted(block["metrics"].items()):
            row = [suite, metric, _fmt(entry["value"]), str(entry["n"]), _fmt_ci(entry["ci95"])]
            if compare is not None:
                other = other_metrics.get(metric)
                if other is None:
                    row += ["-", "-"]
                else:
                    delta = float(entry["value"]) - float(other["value"])
                    row += [_fmt(other["value"]), f"{delta:+.3f}"]
            rows.append(row)
    widths = [max(len(header[c]), *(len(r[c]) for r in rows)) if rows else len(header[c])
              for c in range(len(header))]
    commit = summary.get("commit")
    provenance = ""
    if commit:
        provenance = f"  commit {commit[:12]}" + ("  DIRTY" if summary.get("dirty") else "")
    elif summary.get("dirty") is None and "commit" in summary:
        provenance = "  commit unknown"
    lines = [f"run {summary['run_id']}  ({summary['ts']}){provenance}",
             "  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True)),
             "  ".join("-" * w for w in widths)]
    lines += ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)) for row in rows]
    contention = _contention_line(summary)
    if contention:
        lines.append(contention)
    return "\n".join(lines)
