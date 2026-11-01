"""Explicit units for cost/latency scalarization and nondominated candidates."""

from atsuite.workload.profile import number


def validate_weights(cost_weight: float, latency_weight: float) -> None:
    if (
        not number(cost_weight)
        or not number(latency_weight)
        or cost_weight + latency_weight == 0
    ):
        raise ValueError("Nonnegative cost/latency weights with positive sum required")


def pareto(
    candidates: list[dict], cost="objective_cost", latency="objective_latency"
) -> list[dict]:
    """Return all nondominated points, retaining equivalent deployment choices."""
    points = [c for c in candidates if number(c.get(cost)) and number(c.get(latency))]
    return [
        c
        for c in points
        if not any(
            other[cost] <= c[cost]
            and other[latency] <= c[latency]
            and (other[cost] < c[cost] or other[latency] < c[latency])
            for other in points
        )
    ]
