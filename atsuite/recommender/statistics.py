from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Interval:
    estimate: float
    lower: float | None
    upper: float | None
    confidence_level: float
    method: str


def quantile(values: Iterable[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("quantile requires at least one value")
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between 0 and 1")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def mean_interval(values: Iterable[float], confidence_level: float = 0.95) -> Interval:
    samples = [float(value) for value in values]
    if not samples:
        return Interval(float("nan"), None, None, confidence_level, "unavailable")
    estimate = statistics.mean(samples)
    if len(samples) < 2:
        return Interval(estimate, None, None, confidence_level, "single_sample")
    # Normal approximation is deliberately labelled; use block bootstrap when
    # temporal dependence matters.
    z = 1.96 if confidence_level == 0.95 else 1.645 if confidence_level == 0.90 else 2.576 if confidence_level == 0.99 else 1.96
    half = z * statistics.stdev(samples) / math.sqrt(len(samples))
    return Interval(estimate, max(0.0, estimate - half), estimate + half, confidence_level, "normal_approximation")


def wilson_interval(successes: int, trials: int, confidence_level: float = 0.95) -> Interval:
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("invalid binomial counts")
    if not trials:
        return Interval(float("nan"), None, None, confidence_level, "unavailable")
    z = 1.96 if confidence_level == 0.95 else 1.645 if confidence_level == 0.90 else 2.576 if confidence_level == 0.99 else 1.96
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials) / denominator
    return Interval(p, max(0.0, center - half), min(1.0, center + half), confidence_level, "wilson")


def zero_failure_lower_bound(trials: int, confidence_level: float = 0.95) -> float | None:
    if trials <= 0:
        return None
    return (1 - confidence_level) ** (1 / trials)


def compare_means(left: Iterable[float], right: Iterable[float], *, practical_difference: float = 0.0) -> str:
    a, b = list(left), list(right)
    if not a or not b:
        return "insufficient_evidence"
    difference = statistics.mean(a) - statistics.mean(b)
    if abs(difference) <= practical_difference:
        return "practically_equivalent"
    return "left_lower" if difference < 0 else "right_lower"
