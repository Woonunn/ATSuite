from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Any

from atsuite.recommender.capabilities import ResourceConstraints, ResourceVector, enumerate_legal_resources


@dataclass(frozen=True)
class ResourceCandidate:
    target: str
    vector: ResourceVector
    source: str = "grid"


def sparse_resources(provider: str, region: str = "", deployment_mode: str = "", *, constraints: ResourceConstraints | None = None, limit: int = 16) -> list[ResourceVector]:
    values = enumerate_legal_resources(provider, region, deployment_mode, constraints)
    if len(values) <= limit:
        return values
    # Deterministic coverage of the low, middle, and high resource ranges.
    indices = sorted({0, len(values) - 1, *(round(i * (len(values) - 1) / (limit - 1)) for i in range(limit))})
    return [values[index] for index in indices[:limit]]


def coordinate_search(targets: Iterable[str], provider: str, evaluate: Callable[[dict[str, ResourceVector]], float], *, region: str = "", deployment_mode: str = "", constraints: ResourceConstraints | None = None, limit_per_target: int = 8) -> tuple[dict[str, ResourceVector], float]:
    """Small deterministic coordinate search for resource vectors.

    It intentionally evaluates a bounded legal grid. A predictor can replace
    ``evaluate`` later without changing the capability or candidate contracts.
    """
    current = {target: sparse_resources(provider, region, deployment_mode, constraints=constraints, limit=limit_per_target)[0] for target in targets}
    best = evaluate(current)
    changed = True
    while changed:
        changed = False
        for target in sorted(current):
            for vector in sparse_resources(provider, region, deployment_mode, constraints=constraints, limit=limit_per_target):
                trial = dict(current)
                trial[target] = vector
                score = evaluate(trial)
                if score < best:
                    current, best, changed = trial, score, True
    return current, best
