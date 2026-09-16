from __future__ import annotations

from typing import Any

from atsuite.advisor.model import AdvisorResponse


def template_explanation(recommendation: Any) -> AdvisorResponse:
    winner = getattr(getattr(recommendation, "winner", None), "candidate", None)
    winner_id = getattr(winner, "id", None)
    tradeoffs = []
    if winner_id:
        tradeoffs.append("Deterministic recommendation is based on measured evidence and declared constraints.")
    else:
        tradeoffs.append("No verified winner is available because comparable evidence is insufficient.")
    return AdvisorResponse("Use the frozen objective, constraints, and evidence scope.", preferred_candidate_id=winner_id,
                           reason_codes=("deterministic_fallback",), tradeoffs=tuple(tradeoffs))
