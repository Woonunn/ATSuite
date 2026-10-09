from __future__ import annotations

from dataclasses import dataclass
import copy
from typing import Callable, Iterable, Any

from atsuite.recommender.capabilities import ResourceConstraints, ResourceVector, enumerate_legal_resources


@dataclass(frozen=True)
class ResourceCandidate:
    target: str
    vector: ResourceVector
    source: str = "grid"


def sparse_resources(provider: str, region: str = "", deployment_mode: str = "", *, constraints: ResourceConstraints | None = None, limit: int = 16) -> list[ResourceVector]:
    if limit < 1:
        raise ValueError("resource limit must be positive")
    values = enumerate_legal_resources(provider, region, deployment_mode, constraints)
    if len(values) <= limit:
        return values
    if limit == 1:
        return values[:1]
    # Deterministic coverage of the low, middle, and high resource ranges.
    indices = sorted({0, len(values) - 1, *(round(i * (len(values) - 1) / (limit - 1)) for i in range(limit))})
    return [values[index] for index in indices[:limit]]


def local_resource_candidates(baseline: dict[str, dict[str, Any]], provider: str, *, region: str = "", deployment_mode: str = "", limit_per_target: int = 8):
    """Preserve the exact baseline, then vary one physical target at a time.

    Values never migrate between targets. Runtime options outside CPU/memory
    remain pinned; generated fractional CPU points add their required options.
    This finite deterministic neighborhood is also the ML-disabled fallback.
    """
    yield copy.deepcopy(baseline), {"source": "exact_baseline", "changed_target": None}
    for target in sorted(baseline):
        original = baseline[target]
        constraints = ResourceConstraints(disk_mb=original.get("disk_mb", 512), timeout_s=original.get("timeout_s", 30),
            architectures=(original.get("architecture", "linux/amd64"),))
        for vector in sparse_resources(provider, region, deployment_mode, constraints=constraints, limit=limit_per_target):
            replacement = copy.deepcopy(original)
            replacement.update({"cpu": vector.cpu, "memory_mb": vector.memory_mb})
            replacement["extras"] = {**original.get("extras", {}), **vector.extras}
            if replacement == original:
                continue
            trial = copy.deepcopy(baseline)
            trial[target] = replacement
            yield trial, {"source": "single_target_coordinate", "changed_target": target,
                          "changed_fields": [key for key in replacement if replacement[key] != original.get(key)]}


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
