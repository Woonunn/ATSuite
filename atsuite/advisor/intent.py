from __future__ import annotations
import re
from atsuite.advisor.model import Intent

def parse_intent(text: str) -> Intent:
    raw = str(text or '').strip()
    lower = raw.lower()
    objective = 'balanced'
    if any(token in lower for token in ('最省钱','cheapest','cost','成本','省钱')):
        objective = 'min_cost_under_slo' if any(token in lower for token in ('延迟','latency','p95')) else 'min_cost'
    elif any(token in lower for token in ('最快','latency','延迟')):
        objective = 'min_latency'
    providers = tuple(sorted(set(re.findall(r'\b(?:aws_lambda|aws_agentcore|ali_fc|ali_agentrun|gcp_faas|gcp_mcp|mcp_gateway)\b',lower))))
    families = tuple(family for family in ('faas','session') if family in lower)
    p95 = cost = success = None
    unresolved = []
    match = re.search(r'(?:p95|延迟|latency)[^0-9]*(\d+(?:\.\d+)?)\s*(ms|milliseconds?|毫秒|seconds?|sec|s|秒)?',lower)
    if match:
        number = float(match.group(1))
        unit = match.group(2)
        if unit is None:
            unresolved.append('latency_unit_unspecified')
        else:
            p95 = number * (1000 if unit in {'s','sec','second','seconds','秒'} else 1)
    match = re.search(r'(?:cost|成本|预算)[^0-9]*(\d+(?:\.\d+)?)\s*(usd|美元|\$)',lower)
    if match:
        cost = float(match.group(1))
    elif re.search(r'(?:cost|成本|预算)[^0-9]*\d',lower):
        unresolved.append('cost_currency_unspecified')
    match = re.search(r'(?:success(?: rate)?|成功率)[^0-9]*(\d+(?:\.\d+)?)\s*(%)?',lower)
    if match:
        success = float(match.group(1)) / (100 if match.group(2) else 1)
        if success > 1:
            unresolved.append('invalid_success_rate'); success = None
    return Intent(objective,providers,families,p95,cost,success,raw,tuple(unresolved))
