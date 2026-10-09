from __future__ import annotations

import json
import copy
from dataclasses import asdict, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from atsuite.market_data.fx import load_fx_snapshot
from atsuite.market_data.model import FxSnapshot, PriceTier, RateCard, UsageEntry, UsageLedger
from atsuite.market_data.pricing import BillingRule, estimate_cost
from atsuite.recommender.identity import digest


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def load_usage_ledger(path: str | Path) -> UsageLedger:
    entries = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            values = {key: value for key, value in payload.items() if key in {f.name for f in fields(UsageEntry)}}
            values['quantity'] = _decimal(payload['quantity'])
            entries.append(UsageEntry(**values))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid usage ledger line {line_number}: {exc}") from exc
    return UsageLedger(tuple(entries))


def _rate_card(payload: dict[str, Any]) -> RateCard:
    tiers = tuple(
        PriceTier(
            None if tier.get("up_to") in (None, "", "Inf", "inf") else _decimal(tier["up_to"]),
            _decimal(tier["unit_price"]),
        )
        for tier in payload.get("tiers") or []
    )
    if not tiers:
        raise ValueError(f"rate card {payload.get('sku', '')} has no tiers")
    return RateCard(
        snapshot_id=str(payload["snapshot_id"]),
        provider=str(payload["provider"]),
        service=str(payload.get("service") or payload.get("category") or ""),
        sku=str(payload.get("sku", "")),
        region=str(payload.get("region", "")),
        billing_mode=str(payload.get("billing_mode", "")),
        unit=str(payload.get("unit", "")),
        currency=str(payload["currency"]).upper(),
        tiers=tiers,
        fetched_at=str(payload.get("fetched_at", "")),
        observed_at=str(payload.get("observed_at", "")),
        source=str(payload.get("source", "")),
        freshness=str(payload.get("freshness", "unknown")),
        metadata=dict(payload.get("metadata") or {}),
        **{key: payload[key] for key in ("architecture", "price_scope", "account_id", "effective_from", "effective_to", "source_version") if key in payload},
    )


def load_rate_cards(snapshot: dict[str, Any]) -> list[RateCard]:
    cards = [_rate_card(item) for item in snapshot.get("rate_cards", [])]
    return cards


def _load_fx(snapshot: dict[str, Any], target_currency: str) -> FxSnapshot | None:
    payload = snapshot.get("fx") or snapshot.get("fx_snapshot")
    if not payload:
        return None
    if isinstance(payload, str):
        return load_fx_snapshot(payload)
    return FxSnapshot(**payload)


def reprice(
    experiment: dict[str, Any],
    market_snapshot: dict[str, Any],
    *,
    usage_ledger_path: str | Path,
    target_currency: str = "USD",
    comparison=None,
) -> dict[str, Any]:
    ledger = load_usage_ledger(usage_ledger_path)
    cards = load_rate_cards(market_snapshot)
    rules = {}
    from atsuite.market_data.service import frozen_billing_rules
    rules.update(frozen_billing_rules(market_snapshot))
    for category, raw in (experiment.get("billing_rules") or {}).items():
        rules[str(category)] = BillingRule(
            rounding_seconds=_decimal(raw["rounding_seconds"]) if raw.get("rounding_seconds") is not None else None,
            minimum_quantity=_decimal(raw.get("minimum_quantity", 0)),
            deduplicate=bool(raw.get("deduplicate", True)),
            aggregation_scope=raw.get('aggregation_scope'),
            aggregation_rounding_quantity=_decimal(raw['aggregation_rounding_quantity']) if raw.get('aggregation_rounding_quantity') is not None else None,
            minimum_amount=_decimal(raw['minimum_amount']) if raw.get('minimum_amount') is not None else None,
            minimum_currency=raw.get('minimum_currency'),
        )
    estimates = estimate_cost(
        ledger,
        cards,
        fx=_load_fx(market_snapshot, target_currency),
        target_currency=target_currency,
        rules=rules,
        scope="experiment_spend_estimate",
    )
    missing = sorted(item.category for item in estimates if item.coverage == "missing")
    stale = sorted(item.category for item in estimates if item.coverage == "stale")
    if not ledger.entries:
        status = "insufficient_usage_ledger"
    elif missing:
        status = "reprice_missing_coverage"
    elif stale:
        status = "reprice_stale"
    else:
        status = "reprice_completed"
    costs = []
    for item in estimates:
        row = asdict(item)
        row["amount"] = str(item.amount) if item.amount is not None else None
        row["quantity"] = str(item.quantity)
        costs.append(row)
    candidates = {}
    for item in estimates:
        candidate = candidates.setdefault(item.candidate_id, {"candidate_id": item.candidate_id, "amount": Decimal("0"), "currency": target_currency, "coverage": "available", "cost_basis": "rate_card_estimate", "cost_scope": "experiment_spend_estimate", "source_snapshot_ids": []})
        candidate["source_snapshot_ids"].append(item.source_snapshot_id)
        if item.amount is None:
            candidate["amount"], candidate["coverage"] = None, "missing"
        elif candidate["amount"] is not None:
            candidate["amount"] += item.amount
            if item.coverage != "available":
                candidate["coverage"] = item.coverage
    candidate_costs = [{**row, "amount": str(row["amount"]) if row["amount"] is not None else None} for row in candidates.values()]
    result = {
        "schema_version": 2,
        "candidate_costs": candidate_costs,
        "parent_report_digest": digest(experiment, length=32),
        "recommendation": None,
        "status": status,
        "original_digest": digest(experiment, length=32),
        "market_snapshot_digest": digest(market_snapshot, length=32),
        "usage_ledger_digest": digest([
            {**asdict(entry), "quantity": str(entry.quantity)}
            for entry in ledger.entries
        ], length=32),
        "usage_ledger": str(Path(usage_ledger_path).resolve()),
        "costs": costs,
        "coverage": {"missing": missing, "stale": stale},
        "market_snapshot": market_snapshot.get("snapshot_id", ""),
    }

    if comparison is not None:
        # The caller supplies the canonical service plus resolvable frozen evidence.
        # Never rank naked ledger totals as verified recommendations.
        result['recommendation'] = comparison(experiment, candidate_costs, market_snapshot)
    elif experiment.get('evidence_candidates') or (experiment.get('comparison') or {}).get('evidence_candidates'):
        result['recommendation'] = compare_repriced_evidence(experiment, ledger, market_snapshot, target_currency=target_currency)
    elif experiment.get('candidates'):
        result['recommendation_status'] = 'requires_canonical_evidence_comparison'
    result['report_digest'] = digest(result, length=32)
    return result


def compare_repriced_evidence(experiment, ledger, market_snapshot, *, target_currency='USD'):
    from atsuite.market_data.service import price_candidate_ledger
    from atsuite.recommender.service import compare
    from atsuite.recommender.contracts import ObjectiveSpec,ProtocolSpec
    from atsuite.recommender.eligibility import ComparisonContext
    original=experiment.get('comparison') or experiment
    rows=copy.deepcopy(original.get('evidence_candidates') or experiment.get('evidence_candidates') or [])
    all_entries=[{**asdict(e),'quantity':str(e.quantity)} for e in ledger.entries]
    priced=price_candidate_ledger(all_entries,market_snapshot,target_currency=target_currency)
    by_candidate={cost['candidate_id']:cost for cost in priced['candidate_costs']}
    for row in rows:
        cid=row['candidate_id']
        entries=[e for e in all_entries if e['candidate_id']==cid]
        cost=dict(by_candidate.get(cid,{'amount':None,'currency':target_currency,'coverage':'missing','cost_scope':'declared_runtime_scope'}))
        cost['coverage']='complete' if cost.get('coverage')=='available' else 'missing'
        cost['trial_costs']={entry['run_id']:priced['trial_costs'].get(entry['run_id']) for entry in entries if entry.get('phase')=='confirmation'}
        formal=[e for e in entries if e.get('phase','confirmation')=='confirmation']
        trials=[t for t in row.get('trials',[]) if t.get('phase')=='confirmation']
        successes=sum(t.get('status')=='success' and t.get('output_validation')=='passed' for t in trials)
        artifacts=row.setdefault('artifacts',{})
        identity={key:row[key] for key in ('candidate_id','scenario_id','workload_fingerprint') if key in row}
        refs=[]
        for kind,content in (('usage_ledger',{'entries':formal}),('rate_snapshot',{'snapshot':market_snapshot})):
            artifact={**identity,'kind':kind,**content}
            reference='sha256:'+digest(artifact,length=64)
            artifacts[reference]=artifact;refs.append(reference)
        metric=row['metrics']['cost']
        retained=[ref for ref in metric.get('evidence_refs',[]) if artifacts.get(ref,{}).get('kind')=='confirmation_trials']
        metric.update(value=float(cost['amount'])/successes if cost.get('amount') is not None and successes else None,
                      unit=target_currency,quality='estimated' if cost.get('coverage')=='complete' and successes else 'missing',
                      evidence_refs=retained+refs,method_version='ledger_rate_fx_v1')
        row['cost_scope']=cost.get('cost_scope','declared_runtime_scope')
        row['cost_basis']='rate_card_estimate'
        from atsuite.market_data.statistics import attach_cost_statistics
        attach_cost_statistics(row,cost,confidence_level=(original.get('protocol') or {}).get('confidence_level',.95),candidate_count=len(rows))
    plan_spec=(experiment.get('plan') or {}).get('spec') or {}
    objective=ObjectiveSpec.from_dict({**(original.get('objective') or plan_spec.get('objective') or {}),'base_currency':target_currency})
    protocol=ProtocolSpec.from_dict(original.get('protocol') or plan_spec.get('protocol') or {})
    simulated=bool(rows) and all(row.get('measurement_source')=='simulated' for row in rows)
    context=ComparisonContext(scenario_id=rows[0].get('scenario_id','default') if rows else 'default',workload_fingerprint=rows[0].get('workload_fingerprint','') if rows else '',expected_candidate_ids=tuple(row['candidate_id'] for row in rows),allow_simulated=simulated)
    decision=compare(rows,objective,protocol=protocol,context=context)
    if simulated:
        decision.setdefault('best_simulated',decision['best_verified']);decision['best_verified']=None
        decision['cloud_verified']=False;decision['evidence_quality']='simulated'
    return decision
