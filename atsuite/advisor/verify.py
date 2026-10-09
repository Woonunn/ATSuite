from __future__ import annotations
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import re
from atsuite.advisor.model import AdvisorResponse

def verify_response(response: AdvisorResponse, *, candidate_catalog, eligible_ids, metric_catalog, objective_winner=None, strict=True):
    errors=[]
    for proposal in response.candidate_proposals:
        if proposal.specification:
            # New proposals are validated by workflow.validate_proposal, never eligible yet.
            errors.append('new_proposal_requires_validation')
        elif proposal.candidate_id not in candidate_catalog:
            errors.append(f'unknown candidate: {proposal.candidate_id}')
        elif proposal.candidate_id not in eligible_ids:
            errors.append(f'ineligible candidate: {proposal.candidate_id}')
    if response.preferred_candidate_id:
        if response.preferred_candidate_id not in candidate_catalog or response.preferred_candidate_id not in eligible_ids:
            errors.append('ineligible preferred candidate')
        elif strict and objective_winner and response.preferred_candidate_id != objective_winner:
            errors.append('advisor cannot replace the deterministic objective winner')
    for reference in response.metric_references:
        if reference not in metric_catalog:
            errors.append(f'unknown metric reference: {reference}')
    for claim in response.metric_claims:
        row = metric_catalog.get(claim.get('metric_id'))
        if not isinstance(row,dict):
            errors.append('unknown_metric_claim'); continue
        for field in ('candidate_id','unit','scope','currency'):
            if claim.get(field) != row.get(field):
                errors.append('metric_claim_' + field + '_mismatch')
        try:
            if Decimal(str(claim.get('value'))) != Decimal(str(row.get('value'))):
                errors.append('metric_claim_value_mismatch')
        except (InvalidOperation,ValueError):
            errors.append('invalid_metric_claim_value')
    # Measurements must be represented by structured claims, not unconstrained prose.
    for prose in (response.objective_interpretation,*response.tradeoffs):
        if re.search(r'(?<![A-Za-z_])\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|usd|cny|eur|gb|mb|%)\b',prose,re.I):
            errors.append('unstructured_numeric_claim')
    return replace(response,valid=not errors,validation_errors=tuple(errors))
