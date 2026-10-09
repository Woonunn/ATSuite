from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Intent:
    objective: str = "balanced"
    allowed_providers: tuple[str, ...] = ()
    allowed_families: tuple[str, ...] = ()
    max_p95_latency_ms: float | None = None
    max_cost_usd: float | None = None
    min_success_rate: float | None = None
    explanation: str = ""
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateProposal:
    candidate_id: str
    reason_codes: tuple[str, ...] = ()
    additional_experiments: tuple[str, ...] = ()
    specification: dict[str, Any] = field(default_factory=dict)
    parent_plan_digest: str = ""


@dataclass(frozen=True)
class AdvisorResponse:
    objective_interpretation: str
    candidate_proposals: tuple[CandidateProposal, ...] = ()
    preferred_candidate_id: str | None = None
    reason_codes: tuple[str, ...] = ()
    metric_references: tuple[str, ...] = ()
    tradeoffs: tuple[str, ...] = ()
    additional_experiments: tuple[str, ...] = ()
    provider: str = "template"
    model: str = "deterministic"
    valid: bool = True
    validation_errors: tuple[str, ...] = ()
    audit: dict[str, Any] = field(default_factory=dict)
    metric_claims: tuple[dict[str, Any], ...] = ()
    advisor_alternative: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"objective_interpretation": self.objective_interpretation, "candidate_proposals": [{"candidate_id": p.candidate_id, "reason_codes": list(p.reason_codes), "additional_experiments": list(p.additional_experiments), "specification": p.specification, "parent_plan_digest": p.parent_plan_digest} for p in self.candidate_proposals], "preferred_candidate_id": self.preferred_candidate_id, "reason_codes": list(self.reason_codes), "metric_references": list(self.metric_references), "tradeoffs": list(self.tradeoffs), "additional_experiments": list(self.additional_experiments), "provider": self.provider, "model": self.model, "valid": self.valid, "validation_errors": list(self.validation_errors), "audit": self.audit, "metric_claims": list(self.metric_claims), "advisor_alternative": self.advisor_alternative}
