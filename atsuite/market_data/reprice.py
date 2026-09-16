from __future__ import annotations

import json
from dataclasses import asdict
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
            entries.append(UsageEntry(
                candidate_id=str(payload["candidate_id"]),
                run_id=str(payload["run_id"]),
                category=str(payload["category"]),
                quantity=_decimal(payload["quantity"]),
                unit=str(payload["unit"]),
                provider=str(payload["provider"]),
                resource_id=str(payload.get("resource_id", "")),
                source_ref=str(payload.get("source_ref", "")),
                metadata=dict(payload.get("metadata") or {}),
            ))
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
        service=str(payload.get("category") or payload.get("service") or ""),
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
    )


def load_rate_cards(snapshot: dict[str, Any]) -> dict[tuple[str, str], RateCard]:
    cards = [_rate_card(item) for item in snapshot.get("rate_cards", [])]
    return {(card.provider, card.service): card for card in cards}


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
) -> dict[str, Any]:
    ledger = load_usage_ledger(usage_ledger_path)
    cards = load_rate_cards(market_snapshot)
    rules = {}
    for category, raw in (experiment.get("billing_rules") or {}).items():
        rules[str(category)] = BillingRule(
            rounding_seconds=_decimal(raw["rounding_seconds"]) if raw.get("rounding_seconds") is not None else None,
            minimum_quantity=_decimal(raw.get("minimum_quantity", 0)),
            deduplicate=bool(raw.get("deduplicate", True)),
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
    if missing:
        status = "reprice_missing_coverage"
    elif stale:
        status = "reprice_stale"
    else:
        status = "reprice_completed"
    costs = []
    for item in estimates:
        row = asdict(item)
        row["amount"] = str(item.amount)
        row["quantity"] = str(item.quantity)
        costs.append(row)
    return {
        "schema_version": 1,
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
