from __future__ import annotations
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any


def freshness_at(observed_at: str, fetched_at: str, declared: str = 'fresh', *, now: datetime | None = None, ttl_seconds: int = 86400 * 7) -> str:
    """Recheck timestamps on use; legacy non-date fixture labels remain readable."""
    now = now or datetime.now(timezone.utc)
    if not observed_at or not fetched_at:
        return 'unknown'
    for raw in (observed_at, fetched_at):
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
        except ValueError:
            return 'unknown'
        if (now - value).total_seconds() > ttl_seconds:
            return 'stale'
        if value > now:
            return 'unknown'
    return declared

@dataclass(frozen=True)
class FxSnapshot:
    snapshot_id: str
    base_currency: str
    rates: dict[str, str]
    observed_at: str
    fetched_at: str
    source: str
    source_url: str = ''
    freshness: str = 'fresh'
    observed_precision: str = 'datetime'
    effective_at: str = ''
    source_version: str = '1'
    def rate(self, currency: str) -> Decimal:
        currency = currency.upper()
        if currency == self.base_currency:
            return Decimal('1')
        try:
            value = Decimal(str(self.rates[currency]))
        except KeyError as exc:
            raise ValueError(f'missing FX rate: {currency}') from exc
        if not value.is_finite() or value <= 0:
            raise ValueError('FX rate must be positive and finite')
        return value
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
    freshness: str = 'fresh'
    metadata: dict[str, Any] = field(default_factory=dict)
    architecture: str = ''
    price_scope: str = 'public'
    account_id: str = ''
    effective_from: str = ''
    effective_to: str = ''
    source_version: str = '1'
    def __post_init__(self):
        previous = Decimal('0')
        for index, tier in enumerate(self.tiers):
            if not tier.unit_price.is_finite() or tier.unit_price < 0:
                raise ValueError('price must be finite and nonnegative')
            if tier.up_to is None:
                if index != len(self.tiers) - 1:
                    raise ValueError('unbounded tier must be last')
            elif not tier.up_to.is_finite() or tier.up_to <= previous:
                raise ValueError('price tiers must have increasing finite boundaries')
            else:
                previous = tier.up_to
    def price_for(self, quantity: Decimal) -> Decimal:
        if not quantity.is_finite() or quantity < 0:
            raise ValueError('quantity must be finite and nonnegative')
        remaining, previous, total = quantity, Decimal('0'), Decimal('0')
        for tier in self.tiers:
            upper = tier.up_to if tier.up_to is not None else quantity
            amount = max(Decimal('0'), min(remaining, upper - previous))
            total += amount * tier.unit_price
            remaining -= amount
            previous = upper
            if remaining <= 0:
                break
        if remaining > 0:
            raise ValueError('quantity outside rate-card tiers')
        return total

@dataclass(frozen=True)
class UsageEntry:
    candidate_id: str
    run_id: str
    category: str
    quantity: Decimal
    unit: str
    provider: str
    resource_id: str = ''
    source_ref: str = ''
    metadata: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = ''
    experiment_id: str = ''
    attempt_id: str = ''
    service: str = ''
    account_id: str = ''
    region: str = ''
    sku: str = ''
    architecture: str = ''
    billing_mode: str = ''
    currency: str = ''
    price_scope: str = 'public'
    phase: str = 'confirmation'
    usage_start: str = ''
    usage_end: str = ''
    billing_period: str = ''
    provider_meter_id: str = ''
    schema_version: int = 2
    def __post_init__(self):
        if not isinstance(self.quantity, Decimal) or not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError('usage quantity must be finite nonnegative Decimal')
    def meter_identity(self):
        meter = self.provider_meter_id or self.metadata.get('provider_meter_id') or self.metadata.get('request_id') or self.metadata.get('session_id')
        if not meter:
            return None
        return (self.provider, self.service, self.account_id, self.resource_id, self.category, self.sku, self.billing_period, str(meter))

@dataclass(frozen=True)
class CostEstimate:
    category: str
    amount: Decimal | None
    currency: str
    quantity: Decimal
    unit: str
    coverage: str
    source_snapshot_id: str
    formula: str
    scope: str = 'execution_cost_estimate'
    candidate_id: str = ''
    reason_code: str = ''
    cost_basis: str = 'rate_card_estimate'
    fx_snapshot_id: str = ''
    phase: str = 'confirmation'
    run_id: str = ''
    attempt_id: str = ''

@dataclass(frozen=True)
class UsageLedger:
    entries: tuple[UsageEntry, ...] = ()
    version: int = 1
    parent_digest: str = ''
    def add(self, entry: UsageEntry) -> 'UsageLedger':
        return UsageLedger(self.entries + (entry,), self.version + 1, self.content_digest())
    def content_digest(self) -> str:
        from atsuite.recommender.identity import digest
        return digest([{**asdict(e), 'quantity': str(e.quantity)} for e in self.entries], length=32)
    def deduplicated(self, key_fields=None) -> 'UsageLedger':
        seen, unique = {}, []
        for entry in self.entries:
            key = entry.meter_identity()
            if key is not None and key in seen:
                previous=seen[key]
                if (previous.quantity,previous.unit,previous.candidate_id)!=(entry.quantity,entry.unit,entry.candidate_id):
                    raise ValueError("conflicting_provider_meter_observation")
                continue
            if key is not None:
                seen[key]=entry
            unique.append(entry)
        return UsageLedger(tuple(unique), self.version, self.parent_digest)
    def by_category(self) -> dict[str, Decimal]:
        result = {}
        for entry in self.entries:
            key = (entry.category, entry.unit)
            result[key] = result.get(key, Decimal('0')) + entry.quantity
        return result
