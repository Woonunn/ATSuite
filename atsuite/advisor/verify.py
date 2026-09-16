from __future__ import annotations

from typing import Any

from atsuite.advisor.model import AdvisorResponse


def verify_response(response: AdvisorResponse, *, candidate_catalog: dict[str, Any], eligible_ids: set[str], metric_catalog: dict[str, Any], objective_winner: str | None = None, strict: bool = True) -> AdvisorResponse:
    errors = []
    ids = set(candidate_catalog)
    for proposal in response.candidate_proposals:
        if proposal.candidate_id not in ids:
            errors.append(f"unknown candidate: {proposal.candidate_id}")
        elif proposal.candidate_id not in eligible_ids:
            errors.append(f"ineligible candidate: {proposal.candidate_id}")
    if response.preferred_candidate_id:
        if response.preferred_candidate_id not in ids:
            errors.append(f"unknown preferred candidate: {response.preferred_candidate_id}")
        elif strict and objective_winner and response.preferred_candidate_id != objective_winner:
            errors.append("advisor cannot replace the deterministic objective winner")
    for reference in response.metric_references:
        if reference not in metric_catalog:
            errors.append(f"unknown metric reference: {reference}")
    return AdvisorResponse(response.objective_interpretation, response.candidate_proposals, response.preferred_candidate_id,
                           response.reason_codes, response.metric_references, response.tradeoffs, response.additional_experiments,
                           response.provider, response.model, not errors, tuple(errors), response.audit)
