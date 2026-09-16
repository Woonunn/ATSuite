from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Any



@dataclass(frozen=True)
class FxSnapshot:
    snapshot_id: str
    base_currency: str
    rates: dict[str, str]
    observed_at: str
    fetched_at: str
    source: str
    source_url: str = ""
    freshness: str = "fresh"

    def rate(self, currency: str) -> Decimal:
        currency = currency.upper()
        if currency == self.base_currency:
            return Decimal("1")
        try:
            return Decimal(str(self.rates[currency]))
        except KeyError as exc:
            raise ValueError(f"missing FX rate: {currency}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PriceTier:
    up_to: Decimal | None
    unit_price: Decimal


@dataclass(frozen=True)
class RateCard:
    snapshot_id: str
    provider: str
    service: str
    sku: str
    region: str
    billing_mode: str
    unit: str
    currency: str
    tiers: tuple[PriceTier, ...]
    fetched_at: str
    observed_at: str
    source: str
    freshness: str = "fresh"
    metadata: dict[str, Any] = field(default_factory=dict)

    def price_for(self, quantity: Decimal) -> Decimal:
        if quantity < 0:
            raise ValueError("quantity must be nonnegative")
        remaining = quantity
        previous = Decimal("0")
        total = Decimal("0")
        for tier in self.tiers:
            upper = tier.up_to if tier.up_to is not None else quantity
            amount = max(Decimal("0"), min(remaining, upper - previous))
            total += amount * tier.unit_price
            remaining -= amount
            previous = upper
            if remaining <= 0:
                break
        return total


@dataclass(frozen=True)
class UsageEntry:
    candidate_id: str
    run_id: str
    category: str
    quantity: Decimal
    unit: str
    provider: str
    resource_id: str = ""
    source_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CostEstimate:
    category: str
    amount: Decimal
    currency: str
    quantity: Decimal
    unit: str
    coverage: str
    source_snapshot_id: str
    formula: str
    scope: str = "execution_cost_estimate"


@dataclass(frozen=True)
class UsageLedger:
    entries: tuple[UsageEntry, ...] = ()

    def add(self, entry: UsageEntry) -> "UsageLedger":
        if entry.quantity < 0:
            raise ValueError("usage quantity must be nonnegative")
        return UsageLedger(self.entries + (entry,))

    def deduplicated(self, key_fields=("candidate_id", "run_id", "category", "resource_id", "source_ref")) -> "UsageLedger":
        seen = set()
        unique = []
        for entry in self.entries:
            key = tuple(getattr(entry, field) for field in key_fields)
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        return UsageLedger(tuple(unique))

    def by_category(self) -> dict[str, Decimal]:
        result: dict[str, Decimal] = {}
        for entry in self.entries:
            result[entry.category] = result.get(entry.category, Decimal("0")) + entry.quantity
        return result
