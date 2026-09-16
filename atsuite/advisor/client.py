from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Protocol

from atsuite.advisor.model import AdvisorResponse, CandidateProposal


_SYSTEM_PROMPT = """You explain an ATSuite recommendation using only the supplied redacted catalog.
Return candidate IDs and metric references exactly as listed. Never invent measurements, prices, units, providers, or candidates.
The deterministic objective winner cannot be replaced. Proposals are read-only and must not request deployment or credential access."""


class AdvisorClient(Protocol):
    def recommend(self, catalog: dict[str, Any], prompt: str = "") -> AdvisorResponse: ...


def _template_hash() -> str:
    return hashlib.sha256(_SYSTEM_PROMPT.encode()).hexdigest()


def _redacted_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    allowed = {"eligible_candidates", "objective_winner", "metrics", "candidates", "intent", "decision_status"}
    clean = {key: catalog[key] for key in allowed if key in catalog}
    candidates = clean.get("candidates")
    if isinstance(candidates, dict):
        candidate_fields = {"candidate_id", "provider", "family", "region", "logical_topology", "resources_by_target", "status"}
        clean["candidates"] = {
            str(candidate_id): {key: value for key, value in dict(row).items() if key in candidate_fields}
            for candidate_id, row in candidates.items() if isinstance(row, dict)
        }
    return clean


class TemplateAdvisor:
    def recommend(self, catalog: dict[str, Any], prompt: str = "", *, error: str = "") -> AdvisorResponse:
        winner = catalog.get("objective_winner")
        audit = {"template_hash": _template_hash(), "fallback": True, "error": error}
        return AdvisorResponse(
            "The response is generated from the deterministic report catalog.",
            candidate_proposals=tuple(CandidateProposal(str(row)) for row in catalog.get("eligible_candidates", [])),
            preferred_candidate_id=winner,
            reason_codes=("template_fallback",),
            provider="template",
            model="deterministic",
            audit=audit,
        )


class OpenAIAdvisor:
    """Responses API adapter. Only a redacted evidence catalog is sent."""

    def __init__(self, model: str | None = None, client: Any | None = None):
        self.model = model or os.environ.get("ATSUITE_ADVISOR_MODEL", "")
        self.client = client

    def recommend(self, catalog: dict[str, Any], prompt: str = "") -> AdvisorResponse:
        safe_catalog = _redacted_catalog(catalog)
        if not self.model:
            return TemplateAdvisor().recommend(safe_catalog, prompt, error="ATSUITE_ADVISOR_MODEL is not set")
        try:
            from pydantic import BaseModel, ConfigDict

            class ProposalOutput(BaseModel):
                model_config = ConfigDict(extra="forbid")
                candidate_id: str
                reason_codes: list[str]
                additional_experiments: list[str]

            class AdvisorOutput(BaseModel):
                model_config = ConfigDict(extra="forbid")
                objective_interpretation: str
                candidate_proposals: list[ProposalOutput]
                preferred_candidate_id: str | None
                reason_codes: list[str]
                metric_references: list[str]
                tradeoffs: list[str]
                additional_experiments: list[str]

            client = self.client
            if client is None:
                from openai import OpenAI
                client = OpenAI()
            response = client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({"question": prompt, "catalog": safe_catalog}, ensure_ascii=False, sort_keys=True)},
                ],
                text_format=AdvisorOutput,
                store=False,
            )
            parsed = response.output_parsed
            if parsed is None:
                raise ValueError("structured response was empty or refused")
            usage = getattr(response, "usage", None)
            usage_data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage or {}) if isinstance(usage, dict) else {}
            audit = {
                "response_id": getattr(response, "id", ""),
                "status": str(getattr(response, "status", "")),
                "template_hash": _template_hash(),
                "usage": usage_data,
                "fallback": False,
                "catalog_hash": hashlib.sha256(json.dumps(safe_catalog, sort_keys=True, default=str).encode()).hexdigest(),
            }
            proposals = tuple(CandidateProposal(item.candidate_id, tuple(item.reason_codes), tuple(item.additional_experiments)) for item in parsed.candidate_proposals)
            return AdvisorResponse(parsed.objective_interpretation, proposals, parsed.preferred_candidate_id,
                                   tuple(parsed.reason_codes), tuple(parsed.metric_references), tuple(parsed.tradeoffs),
                                   tuple(parsed.additional_experiments), "openai", self.model, audit=audit)
        except Exception as exc:
            return TemplateAdvisor().recommend(safe_catalog, prompt, error=f"{type(exc).__name__}: {exc}")
