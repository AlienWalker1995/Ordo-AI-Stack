"""Confidence intervals for the two kinds of metric the harness reports.

  * a RATE (a mean of 0/1 outcomes)  -> Wilson score interval: well-behaved at small n and at 0 or 1,
    where the normal approximation collapses to a zero-width interval.
  * a MEAN of bounded scores (token counts, 0..1 judge scores) -> normal approximation,
    mean +/- 1.96 * s / sqrt(n); None when n < 2 (no spread to estimate).
"""
from __future__ import annotations

import math
from collections.abc import Sequence

Z95 = 1.959963984540054


def wilson_ci95(successes: float, n: int) -> list[float] | None:
    """95% Wilson interval for `successes` out of `n`, as [low, high]; None when n == 0."""
    if n <= 0:
        return None
    p = successes / n
    denominator = 1 + Z95 * Z95 / n
    centre = (p + Z95 * Z95 / (2 * n)) / denominator
    half = (Z95 * math.sqrt(p * (1 - p) / n + Z95 * Z95 / (4 * n * n))) / denominator
    return [round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6)]


def mean_ci95(values: Sequence[float]) -> list[float] | None:
    """95% normal-approximation interval for the mean of `values`; None when fewer than 2 values."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = Z95 * math.sqrt(variance / n)
    return [round(mean - half, 6), round(mean + half, 6)]
