from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


STAGES = ("PLANNED", "APPROVED", "BUILDING", "DEPLOYING", "EXPLORING", "CONFIRMING", "ANALYZING", "COMPLETED")
TERMINAL_STAGES = {"COMPLETED", "FAILED", "CANCELLED"}
_ALLOWED = {
    "CREATED": {"VALIDATED", "PLANNED", "CANCEL_REQUESTED", "FAILED"},
    "VALIDATED": {"PLANNED", "CANCEL_REQUESTED", "FAILED"},
    "PLANNED": {"PLANNED", "APPROVED", "CANCEL_REQUESTED", "FAILED"},
    "APPROVED": {"BUILDING", "CANCEL_REQUESTED", "FAILED", "CLEANUP_PENDING"},
    "BUILDING": {"DEPLOYING", "CANCEL_REQUESTED", "FAILED", "CLEANUP_PENDING"},
    "DEPLOYING": {"EXPLORING", "CANCEL_REQUESTED", "FAILED", "CLEANUP_PENDING"},
    "EXPLORING": {"CONFIRMING", "ANALYZING", "CANCEL_REQUESTED", "FAILED", "CLEANUP_PENDING"},
    "CONFIRMING": {"ANALYZING", "CANCEL_REQUESTED", "FAILED", "CLEANUP_PENDING"},
    "ANALYZING": {"COMPLETED", "CANCEL_REQUESTED", "FAILED", "CLEANUP_PENDING"},
    "CANCEL_REQUESTED": {"CANCELLED", "CLEANUP_PENDING", "FAILED"},
    "CLEANUP_PENDING": {"COMPLETED", "CANCELLED", "FAILED"},
    "FAILED": {"APPROVED", "BUILDING", "DEPLOYING", "EXPLORING", "CONFIRMING", "ANALYZING", "CLEANUP_PENDING", "FAILED"},
    "CANCELLED": {"APPROVED", "CANCELLED"},
    "COMPLETED": {"COMPLETED"},
}


def validate_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in _ALLOWED.get(current, set()):
        raise ValueError(f"invalid experiment transition: {current} -> {target}")


def next_resumable_stage(record: Mapping[str, Any]) -> str:
    state = str(record.get("state") or "PLANNED")
    completed = set(record.get("completed_stages") or ())
    if state == "COMPLETED":
        return "COMPLETED"
    for stage in STAGES[1:]:
        if stage not in completed:
            return stage
    return "COMPLETED"


def select_exploration(candidates: Iterable[Mapping[str, Any]], limit: int) -> list[str]:
    """Cover each provider/family before filling from predictor rank."""
    if limit < 1:
        return []
    rows = sorted(candidates, key=lambda row: (
        float(row.get("predicted_score", float("inf"))),
        -float(row.get("uncertainty", 0.0)),
        str(row.get("candidate_id", "")),
    ))
    selected: list[str] = []
    covered: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row.get("provider", "")), str(row.get("family", "")))
        candidate_id = str(row.get("candidate_id", ""))
        if candidate_id and key not in covered:
            selected.append(candidate_id)
            covered.add(key)
            if len(selected) == limit:
                return selected
    for row in rows:
        candidate_id = str(row.get("candidate_id", ""))
        if candidate_id and candidate_id not in selected:
            selected.append(candidate_id)
            if len(selected) == limit:
                break
    return selected


def select_confirmation(results: Iterable[Mapping[str, Any]], limit: int = 3) -> list[str]:
    eligible = [row for row in results if row.get("feasible") and row.get("evidence_quality") == "measured"]
    eligible.sort(key=lambda row: (float(row.get("score", float("inf"))), str(row.get("candidate_id", ""))))
    return [str(row["candidate_id"]) for row in eligible[:max(0, limit)]]


@dataclass
class SearchOrchestrator:
    store: Any

    def transition(self, experiment_id: str, target: str, **updates: Any) -> dict[str, Any]:
        record = self.store.get(experiment_id)
        validate_transition(record["state"], target)
        completed = list(record.get("completed_stages") or ())
        if record["state"] in STAGES and record["state"] not in completed:
            completed.append(record["state"])
        return self.store.transition(experiment_id, target, completed_stages=completed, **updates)

    def resume(self, experiment_id: str) -> dict[str, Any]:
        record = self.store.get(experiment_id)
        if record["state"] == "CLEANUP_PENDING":
            return self.store.transition(
                experiment_id, "CLEANUP_PENDING", resumed=True
            )
        stage = next_resumable_stage(record)
        if stage == "COMPLETED":
            return record
        return self.transition(experiment_id, stage, resumed=True)
