"""Confidence intervals for the two kinds of metric the harness reports.

  * a RATE (a mean of 0/1 outcomes)  -> Wilson score interval: well-behaved at small n and at 0 or 1,
    where the normal approximation collapses to a zero-width interval.
  * a MEAN of bounded scores (token counts, wall time, 0..1 judge scores) -> normal approximation,
    mean +/- 1.96 * s / sqrt(n), clamped to the metric's own domain; None when n < 2 (no spread to
    estimate).

A normal-approximation interval has no notion of the domain a mean is drawn from: at small n and high
variance its half-width can exceed the domain in either direction (a token-count mean's lower bound
going negative, a 0..1 judge-score mean's upper bound exceeding 1). That is not a wider-than-usual
interval, it is a nonsense one: the true mean cannot be outside the domain, so an interval that claims
otherwise is wrong, not merely imprecise. `mean_ci95` takes the metric's own bounds and clips the
computed interval to them - the interval narrows toward the boundary instead of extending past it.
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


def mean_ci95(values: Sequence[float], *, lower: float | None = None, upper: float | None = None) -> list[float] | None:
    """95% normal-approximation interval for the mean of `values`, clamped to [`lower`, `upper`]
    (either bound may be omitted for an unbounded count/time metric); None when fewer than 2 values."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = Z95 * math.sqrt(variance / n)
    low, high = mean - half, mean + half
    if lower is not None:
        low = max(low, lower)
    if upper is not None:
        high = min(high, upper)
    return [round(low, 6), round(high, 6)]
