from __future__ import annotations

import re

from atsuite.advisor.model import Intent


def parse_intent(text: str) -> Intent:
    raw = str(text or "").strip()
    lower = raw.lower()
    objective = "balanced"
    if "最省钱" in raw or "cheapest" in lower or "cost" in lower:
        objective = "min_cost_under_slo" if "延迟" in raw or "latency" in lower or "p95" in lower else "min_cost"
    elif "最快" in raw or "latency" in lower or "延迟" in raw:
        objective = "min_latency"
    providers = tuple(sorted(set(re.findall(r"\b(?:aws_lambda|aws_agentcore|ali_fc|ali_agentrun|gcp_faas|gcp_mcp|mcp_gateway)\b", lower))))
    families = tuple(family for family in ("faas", "session") if family in lower)
    p95 = None
    match = re.search(r"(?:p95|延迟)[^0-9]*(\d+(?:\.\d+)?)", lower)
    if match:
        p95 = float(match.group(1))
    return Intent(objective, providers, families, p95, None, None, raw)
