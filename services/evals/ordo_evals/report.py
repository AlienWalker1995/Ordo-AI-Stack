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
    lines = [f"run {summary['run_id']}  ({summary['ts']})",
             "  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True)),
             "  ".join("-" * w for w in widths)]
    lines += ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)) for row in rows]
    return "\n".join(lines)
