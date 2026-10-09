from __future__ import annotations
from decimal import Decimal, ROUND_UP
from datetime import datetime,timezone
from dataclasses import replace
from collections.abc import Mapping
from atsuite.market_data.model import CostEstimate, FxSnapshot, RateCard, UsageLedger, freshness_at

_UNIT_FACTORS = {'ms': ('time', Decimal('.001')), 's': ('time', Decimal('1')), 'second': ('time', Decimal('1')), 'seconds': ('time', Decimal('1')), 'hour': ('time', Decimal('3600')), 'GB-s': ('memory_time', Decimal('1')), 'GB-seconds': ('memory_time', Decimal('1')), 'MB-s': ('memory_time', Decimal('1')/Decimal('1024'))}
UNIT_CONVERSION_VERSION = '1'

def convert_quantity(quantity, source, target):
    if source == target:
        return quantity
    left, right = _UNIT_FACTORS.get(source), _UNIT_FACTORS.get(target)
    if not left or not right or left[0] != right[0]:
        raise ValueError('incompatible_usage_unit')
    return quantity * left[1] / right[1]

class BillingRule:
    def __init__(self, *, rounding_seconds=None, minimum_quantity=Decimal('0'), deduplicate=True,aggregation_scope=None,aggregation_rounding_quantity=None,minimum_amount=None,minimum_currency=None):
        self.rounding_seconds = rounding_seconds
        self.minimum_quantity = minimum_quantity
        self.deduplicate = deduplicate
        self.aggregation_scope=aggregation_scope
        self.aggregation_rounding_quantity=aggregation_rounding_quantity
        self.minimum_amount=minimum_amount
        self.minimum_currency=minimum_currency
        if minimum_quantity < 0 or (rounding_seconds is not None and rounding_seconds <= 0):
            raise ValueError('invalid billing rule')
        if aggregation_scope not in {None,'resource_hour'} or (aggregation_rounding_quantity is not None and aggregation_rounding_quantity<=0) or (minimum_amount is not None and (minimum_amount<0 or not minimum_currency or aggregation_scope is None)):
            raise ValueError('invalid aggregate billing rule')
    def quantity(self, value):
        value = max(value, self.minimum_quantity)
        if self.rounding_seconds and value:
            value = (value / self.rounding_seconds).to_integral_value(rounding=ROUND_UP) * self.rounding_seconds
        return value

def _billing_bucket(entry,rule):
    if rule.aggregation_scope is None:return None
    if not entry.resource_id or not entry.usage_start:return None
    try:
        start=datetime.fromisoformat(entry.usage_start.replace('Z','+00:00'))
        end=datetime.fromisoformat(entry.usage_end.replace('Z','+00:00')) if entry.usage_end else start
        if start.tzinfo is None or end.tzinfo is None:return None
        hour=start.astimezone(timezone.utc).replace(minute=0,second=0,microsecond=0)
        # Spanning-hour usage needs provider interval splitting, never guessed.
        if end.astimezone(timezone.utc)>hour.replace(minute=59,second=59,microsecond=999999):return None
        return (entry.provider,entry.service,entry.region,entry.account_id,entry.resource_id,entry.category,entry.sku,hour.isoformat())
    except ValueError:return None

def _matches(entry, card):
    if entry.provider != card.provider:
        return False
    for field in ('service', 'region', 'sku', 'architecture', 'billing_mode', 'currency', 'price_scope', 'account_id'):
        value = getattr(entry, field)
        card_value = getattr(card, field)
        if field == 'account_id' and card.price_scope == 'public' and not card_value:
            continue
        if field == 'region' and value and card_value in {'global','multi-region'} and value in card.metadata.get('regions',[]):
            continue
        if value and card_value != value:
            return False
    if entry.usage_start:
        if card.effective_from and entry.usage_start < card.effective_from:
            return False
        if card.effective_to and entry.usage_start >= card.effective_to:
            return False
    return True

def estimate_cost(ledger: UsageLedger, cards, *, fx=None, target_currency='USD', rules=None, scope='execution_cost_estimate', account_usage=None):
    """Round individual billable events, then apply shared account-period tiers.

    account_usage maps (account, provider, service, sku, period, snapshot) to
    already-billed quantity. Candidate allocation never resets monthly tiers.
    Legacy two-key mappings remain readable; new callers supply full cards.
    """
    catalog = list(cards.values()) if isinstance(cards, Mapping) else list(cards)
    unique_cards = {c.snapshot_id: c for c in catalog}
    if len(unique_cards) != len(catalog):
        for c in catalog:
            if unique_cards[c.snapshot_id] != c:
                raise ValueError('rate_snapshot_collision')
    consumed = dict(account_usage or {})
    grouped, seen = {}, {}
    prepared=[];buckets={};events=[]
    # Resolve event quantities before tier consumption so hourly resource CU
    # rounding cannot reset for each trial or candidate.
    for entry in ledger.entries:
        rule = (rules or {}).get(entry.category, BillingRule())
        identity = entry.meter_identity()
        if rule.deduplicate and identity is not None:
            if identity in seen:
                previous=seen[identity]
                if (previous.quantity,previous.unit,previous.candidate_id)!=(entry.quantity,entry.unit,entry.candidate_id):
                    raise ValueError("conflicting_provider_meter_observation")
                continue
            seen[identity]=entry
        # Narrow old mapping by category, without erasing full selectors.
        subset = [cards[(entry.provider, entry.category)]] if isinstance(cards, Mapping) and (entry.provider, entry.category) in cards else list(unique_cards.values())
        applicable = [c for c in subset if _matches(entry, c)]
        matched=[]
        for candidate in applicable:
            try:convert_quantity(entry.quantity,entry.unit,candidate.unit)
            except ValueError:continue
            matched.append(candidate)
        reason, card, amount, quantity = '', None, None, entry.quantity
        if len(matched) != 1:
            reason = 'ambiguous_price_match' if matched else 'incompatible_usage_unit' if applicable else 'missing_price'
        else:
            card = matched[0]
            try:
                quantity = rule.quantity(convert_quantity(entry.quantity, entry.unit, card.unit))
                bucket=_billing_bucket(entry,rule)
                if rule.aggregation_scope and bucket is None:raise ValueError('missing_billing_interval_or_resource')
            except ValueError as exc:amount,reason=None,str(exc)
        prepared.append([entry,rule,card,quantity,reason])
        if card and not reason and rule.aggregation_scope:
            buckets.setdefault(_billing_bucket(entry,rule),[]).append(len(prepared)-1)
    for indices in buckets.values():
        rule=prepared[indices[0]][1];total=sum((prepared[i][3] for i in indices),Decimal('0'))
        if rule.aggregation_rounding_quantity:
            quantum=rule.aggregation_rounding_quantity
            rounded=(total/quantum).to_integral_value(rounding=ROUND_UP)*quantum
            assigned=Decimal('0')
            for position,i in enumerate(indices):
                value=(rounded-assigned) if position==len(indices)-1 else rounded*prepared[i][3]/total if total else Decimal('0')
                prepared[i][3]=value;assigned+=value
    for entry,rule,card,quantity,reason in prepared:
        amount=None
        if card and not reason:
            try:
                period_key = (entry.account_id, entry.provider, card.service, card.sku, entry.billing_period, card.snapshot_id)
                previous = consumed.get(period_key, Decimal('0'))
                amount = card.price_for(previous + quantity) - card.price_for(previous)
                consumed[period_key] = previous + quantity
                if card.currency != target_currency:
                    if fx is None:
                        raise ValueError('missing_fx')
                    amount *= fx.rate(target_currency) / fx.rate(card.currency)
            except ValueError as exc:
                amount, reason = None, str(exc)
        freshness = freshness_at(card.observed_at, card.fetched_at, card.freshness) if card else 'unknown'
        if fx is not None and card and card.currency != target_currency:
            fx_status = freshness_at(fx.observed_at, fx.fetched_at, fx.freshness)
            if fx_status != 'fresh':
                freshness = fx_status
        coverage = 'missing' if amount is None else ('available' if freshness == 'fresh' else 'stale')
        events.append([entry,rule,card,CostEstimate(entry.category,amount,target_currency,quantity,card.unit if card else entry.unit,coverage,card.snapshot_id if card else '',reason or f'per-event rounding; account-period marginal tiers; conversion v{UNIT_CONVERSION_VERSION}',scope,entry.candidate_id,reason,fx_snapshot_id=fx.snapshot_id if fx else '',phase=entry.phase,run_id=entry.run_id,attempt_id=entry.attempt_id)])
    for indices in buckets.values():
        rule=events[indices[0]][1]
        if rule.minimum_amount is None or any(events[i][3].amount is None for i in indices):continue
        try:
            minimum=rule.minimum_amount
            if rule.minimum_currency!=target_currency:
                if fx is None:raise ValueError('missing_fx')
                minimum*=fx.rate(target_currency)/fx.rate(rule.minimum_currency)
                if freshness_at(fx.observed_at,fx.fetched_at,fx.freshness)!='fresh':
                    for i in indices:events[i][3]=replace(events[i][3],coverage='stale')
            total=sum((events[i][3].amount for i in indices),Decimal('0'))
            quantity=sum((events[i][3].quantity for i in indices),Decimal('0'))
            if total<minimum:
                assigned=Decimal('0')
                for position,i in enumerate(indices):
                    item=events[i][3]
                    share=item.quantity/quantity if quantity else Decimal('1') if position==0 else Decimal('0')
                    extra=(minimum-total-assigned) if position==len(indices)-1 else (minimum-total)*share
                    assigned+=extra
                    events[i][3]=replace(item,amount=item.amount+extra,formula=item.formula+'; resource-hour CU rounding/minimum proportional quantity allocation v1')
        except ValueError as exc:
            for i in indices:events[i][3]=replace(events[i][3],amount=None,coverage='missing',reason_code=str(exc))
    for entry,rule,card,item in events:
        amount,quantity,reason,coverage=item.amount,item.quantity,item.reason_code,item.coverage
        key = (entry.candidate_id, entry.category, card.snapshot_id if card else '', entry.unit, reason, coverage, entry.phase, entry.run_id, entry.attempt_id)
        current = grouped.get(key)
        if current:
            grouped[key] = replace(current, amount=None if amount is None else current.amount + amount, quantity=current.quantity + quantity)
        else:
            grouped[key] = item
    return list(grouped.values())

def projected_monthly_cost(execution_cost, workflows_per_month, concurrency=1, session_idle_seconds=Decimal('0'), *, idle_rate_per_second=None, storage_cost=Decimal('0')):
    if workflows_per_month < 0 or concurrency < 1:
        raise ValueError('invalid monthly workload')
    if session_idle_seconds and idle_rate_per_second is None:
        raise ValueError('idle duration requires a monetary rate')
    return execution_cost * Decimal(workflows_per_month) + session_idle_seconds * (idle_rate_per_second or Decimal('0')) + storage_cost

def cost_per_successful_workflow(costs, successful_workflows):
    if isinstance(successful_workflows, bool) or not isinstance(successful_workflows, int) or successful_workflows < 0:
        raise ValueError('successful_workflows must be nonnegative integer')
    if successful_workflows == 0 or any(c.amount is None for c in costs):
        return None
    return sum((c.amount for c in costs), Decimal('0')) / Decimal(successful_workflows)
