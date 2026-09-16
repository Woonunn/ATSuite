from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_UP
from typing import Iterable

from atsuite.market_data.model import CostEstimate, FxSnapshot, RateCard, UsageEntry, UsageLedger


class BillingRule:
    def __init__(self, *, rounding_seconds: Decimal | None = None, minimum_quantity: Decimal = Decimal("0"), deduplicate: bool = True):
        self.rounding_seconds = rounding_seconds
        self.minimum_quantity = minimum_quantity
        self.deduplicate = deduplicate

    def quantity(self, value: Decimal) -> Decimal:
        value = max(value, self.minimum_quantity)
        if self.rounding_seconds and value:
            value = (value / self.rounding_seconds).to_integral_value(rounding=ROUND_UP) * self.rounding_seconds
        return value


def estimate_cost(ledger: UsageLedger, cards: dict[tuple[str, str], RateCard], *, fx: FxSnapshot | None = None, target_currency: str = "USD", rules: dict[str, BillingRule] | None = None, scope: str = "execution_cost_estimate") -> list[CostEstimate]:
    grouped: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    units: dict[tuple[str, str, str], str] = {}
    seen = set()
    for entry in ledger.entries:
        rule = (rules or {}).get(entry.category, BillingRule())
        identity = (entry.candidate_id, entry.run_id, entry.category, entry.resource_id, entry.source_ref)
        if rule.deduplicate and identity in seen:
            continue
        if rule.deduplicate:
            seen.add(identity)
        key = (entry.provider, entry.category, entry.unit)
        grouped[key] += entry.quantity
        units[key] = entry.unit
    estimates = []
    for (provider, category, unit), raw_quantity in sorted(grouped.items()):
        card = cards.get((provider, category))
        if card is None:
            estimates.append(CostEstimate(category, Decimal("0"), target_currency, raw_quantity, unit, "missing", "", "rate card unavailable", scope))
            continue
        rule = (rules or {}).get(category, BillingRule())
        quantity = rule.quantity(raw_quantity)
        if card.unit != unit:
            estimates.append(CostEstimate(
                category, Decimal("0"), target_currency, quantity, unit, "missing",
                card.snapshot_id, f"usage unit {unit} does not match rate-card unit {card.unit}", scope,
            ))
            continue
        amount = card.price_for(quantity)
        if card.currency != target_currency:
            if fx is None:
                estimates.append(CostEstimate(
                    category, Decimal("0"), target_currency, quantity, unit, "missing",
                    card.snapshot_id, f"FX unavailable for {card.currency}->{target_currency}", scope,
                ))
                continue
            try:
                amount *= fx.rate(target_currency) / fx.rate(card.currency)
            except ValueError:
                estimates.append(CostEstimate(
                    category, Decimal("0"), target_currency, quantity, unit, "missing",
                    card.snapshot_id, f"FX rate unavailable for {card.currency}->{target_currency}", scope,
                ))
                continue
        estimates.append(CostEstimate(category, amount, target_currency, quantity, unit, "available" if card.freshness == "fresh" else "stale",
                                       card.snapshot_id, f"{quantity} {unit} * {card.currency} rate card", scope))
    return estimates


def projected_monthly_cost(execution_cost: Decimal, workflows_per_month: int, concurrency: int = 1, session_idle_seconds: Decimal = Decimal("0")) -> Decimal:
    if workflows_per_month < 0 or concurrency < 1:
        raise ValueError("invalid monthly workload")
    return execution_cost * Decimal(workflows_per_month) * Decimal(concurrency) + session_idle_seconds
