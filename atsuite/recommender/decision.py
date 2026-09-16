from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from atsuite.recommender.contracts import EvidenceMetric, ObjectiveSpec


DEFAULT_ANCHORS = {
    "p95_workflow_ms": 10_000.0,
    "cost": 1.0,
    "failure_rate": 1.0,
    "memory_mb": 32_768.0,
}


@dataclass(frozen=True)
class DecisionResult:
    status: str
    best_verified: str | None
    pareto_frontier: tuple[str, ...]
    rankings: tuple[dict[str, Any], ...]
    excluded: tuple[dict[str, Any], ...]
    promising_unverified: tuple[str, ...]
    reason: str
    anchors: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _metric(raw: EvidenceMetric | Mapping[str, Any] | None) -> EvidenceMetric | None:
    if raw is None:
        return None
    if isinstance(raw, EvidenceMetric):
        return raw
    return EvidenceMetric(**dict(raw))


def _required(objective: ObjectiveSpec) -> tuple[str, ...]:
    if objective.type == "min_cost_under_slo":
        names = ["cost", "p95_workflow_ms", "failure_rate"]
    elif objective.type == "min_latency":
        names = ["p95_workflow_ms", "failure_rate"]
    elif objective.type == "min_cost":
        names = ["cost", "failure_rate"]
    else:
        names = ["p95_workflow_ms", "cost", "failure_rate"]
    return tuple(names)


def _weights(objective: ObjectiveSpec) -> dict[str, float]:
    return {
        "min_cost_under_slo": {"cost": 1.0},
        "min_latency": {"p95_workflow_ms": 0.9, "failure_rate": 0.1},
        "min_cost": {"cost": 0.9, "failure_rate": 0.1},
        "balanced": {"p95_workflow_ms": 0.45, "cost": 0.35, "failure_rate": 0.2},
    }[objective.type]


def _constraint_reasons(metrics: Mapping[str, EvidenceMetric], objective: ObjectiveSpec) -> list[str]:
    reasons = []
    latency = metrics.get("p95_workflow_ms")
    failure = metrics.get("failure_rate")
    if objective.max_p95_workflow_ms is not None and latency and latency.value is not None and latency.value > objective.max_p95_workflow_ms:
        reasons.append(f"p95_workflow_ms {latency.value} exceeds {objective.max_p95_workflow_ms}")
    if objective.min_success_rate is not None and failure and failure.value is not None:
        success = 1.0 - failure.value
        if success < objective.min_success_rate:
            reasons.append(f"success_rate {success} below {objective.min_success_rate}")
    return reasons


def _dominates(left: Mapping[str, EvidenceMetric], right: Mapping[str, EvidenceMetric], dimensions: Iterable[str]) -> bool:
    names = tuple(dimensions)
    if not names:
        return False
    return all(left[name].value <= right[name].value for name in names) and any(left[name].value < right[name].value for name in names)


def decide(
    candidates: Iterable[Mapping[str, Any]],
    objective: ObjectiveSpec,
    *,
    anchors: Mapping[str, float] | None = None,
    minimum_score_margin: float = 0.02,
) -> DecisionResult:
    """Return a deterministic recommendation from frozen, comparable evidence."""
    fixed_anchors = {**DEFAULT_ANCHORS, **dict(anchors or {})}
    if any(value <= 0 for value in fixed_anchors.values()):
        raise ValueError("decision anchors must be positive")
    required = _required(objective)
    weights = _weights(objective)
    comparable: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    promising: list[str] = []
    scenario: str | None = None
    complete_evidence_candidates = 0

    for raw in candidates:
        candidate_id = str(raw.get("candidate_id") or "")
        if not candidate_id:
            raise ValueError("candidate_id is required")
        candidate_scenario = str(raw.get("scenario_id") or "default")
        if scenario is None:
            scenario = candidate_scenario
        if candidate_scenario != scenario:
            excluded.append({"candidate_id": candidate_id, "reasons": ["scenario mismatch"]})
            continue
        metrics = {name: value for name, value in ((_name, _metric(_raw)) for _name, _raw in dict(raw.get("metrics") or {}).items()) if value is not None}
        evidence_reasons = []
        for name in required:
            value = metrics.get(name)
            if value is None or value.value is None:
                evidence_reasons.append(f"required metric {name} is missing")
            elif value.quality != "available":
                evidence_reasons.append(f"required metric {name} quality is {value.quality}")
            elif value.measurement_kind != "measured":
                evidence_reasons.append(f"required metric {name} is {value.measurement_kind}")
            elif objective.strict_evidence and not value.evidence_refs:
                evidence_reasons.append(f"required metric {name} has no evidence reference")
        if not evidence_reasons:
            complete_evidence_candidates += 1
        reasons = evidence_reasons + _constraint_reasons(metrics, objective)
        if reasons:
            if any(value.measurement_kind == "predicted" for value in metrics.values()):
                promising.append(candidate_id)
            excluded.append({"candidate_id": candidate_id, "reasons": reasons})
            continue
        score = sum(weights[name] * min(float(metrics[name].value) / fixed_anchors[name], 1.0) for name in weights)
        comparable.append({"candidate_id": candidate_id, "metrics": metrics, "score": score})

    if not comparable:
        status = "no_feasible_candidate" if complete_evidence_candidates else "insufficient_evidence"
        reason = "Complete measured candidates violate the declared constraints." if complete_evidence_candidates else "No candidate has complete measured evidence."
        return DecisionResult(status, None, (), (), tuple(excluded), tuple(sorted(set(promising))), reason, fixed_anchors)

    dimensions = tuple(weights)
    frontier = tuple(sorted(row["candidate_id"] for row in comparable if not any(
        other is not row and _dominates(other["metrics"], row["metrics"], dimensions) for other in comparable
    )))
    ranking_rows = sorted(comparable, key=lambda row: (row["score"], row["candidate_id"]))
    rankings = tuple({"candidate_id": row["candidate_id"], "score": row["score"], "on_pareto_frontier": row["candidate_id"] in frontier} for row in ranking_rows)
    if len(ranking_rows) > 1 and ranking_rows[1]["score"] - ranking_rows[0]["score"] < minimum_score_margin:
        return DecisionResult("no_clear_winner", None, frontier, rankings, tuple(excluded), tuple(sorted(set(promising))),
                              "The leading candidates are within the declared practical score margin.", fixed_anchors)
    winner = ranking_rows[0]["candidate_id"]
    return DecisionResult("recommended", winner, frontier, rankings, tuple(excluded), tuple(sorted(set(promising))),
                          "Winner selected from complete measured evidence using frozen objective anchors.", fixed_anchors)
