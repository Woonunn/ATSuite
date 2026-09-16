from __future__ import annotations

from atsuite.advisor.client import TemplateAdvisor
from atsuite.advisor.intent import parse_intent
from atsuite.advisor.verify import verify_response


def plan_from_text(text: str, catalog: dict, *, client=None) -> dict:
    intent = parse_intent(text)
    client = client or TemplateAdvisor()
    response = client.recommend({**catalog, "intent": intent.__dict__}, text)
    checked = verify_response(response, candidate_catalog=catalog.get("candidates", {}), eligible_ids=set(catalog.get("eligible_candidates", [])), metric_catalog=catalog.get("metrics", {}), objective_winner=catalog.get("objective_winner"))
    return {"intent": intent.__dict__, "response": checked.to_dict()}
