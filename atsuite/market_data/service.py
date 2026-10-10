"""Default online catalog discovery and immutable market freeze.

API boundaries are injectable for contracts. Missing/ambiguous catalogs are
coverage gaps, never old static prices or silently fresh cache entries.
"""
from __future__ import annotations
from dataclasses import asdict, replace
from decimal import Decimal
import json
import urllib.parse
import urllib.request
from atsuite.market_data.fx import ECBFxProvider, load_fx_snapshot
from atsuite.market_data.model import freshness_at
from atsuite.market_data.providers.aws import AWSPriceAdapter
from atsuite.market_data.providers.gcp import GCPCatalogAdapter
from atsuite.market_data.providers.aliyun import AlibabaPriceAdapter
from atsuite.recommender.identity import digest

SERVICE_SELECTORS = {
    'aws_lambda': {'vendor':'aws','service_aliases':('awslambda',),'meters':('request','duration','storage','network')},
    'aws_agentcore': {'vendor':'aws','service_aliases':('amazonbedrockagentcore','bedrockagentcore'),'meters':('cpu','memory','session_idle','storage','network')},
    'gcp_faas': {'vendor':'gcp','service_aliases':('cloud run',),'meters':('request','cpu','memory','storage','network')},
    'gcp_mcp': {'vendor':'gcp','service_aliases':('cloud run',),'meters':('cpu','memory','session_idle','storage','network')},
    'ali_fc': {'vendor':'ali','service_aliases':('function compute','函数计算','fc'),'meters':('compute_units','storage','network')},
    'ali_agentrun': {'vendor':'ali','service_aliases':('function compute','函数计算','fc'),'meters':('compute_units','session_idle','storage','network')},
}

FC_AGGREGATE_RULE={'version':'2026-08-25','source_url':'https://www.alibabacloud.com/help/en/functioncompute/pay-as-you-go-billing-methods','aggregation_scope':'resource_hour','aggregation_rounding_quantity':'1','minimum_amount':'.01','minimum_currency':'USD','allocation':'proportional_billable_quantity_v1'}

def frozen_billing_rules(snapshot):
    from atsuite.market_data.pricing import BillingRule
    supported={'rounding_seconds','minimum_quantity','deduplicate','aggregation_scope','aggregation_rounding_quantity','minimum_amount','minimum_currency'}
    numeric={'rounding_seconds','minimum_quantity','aggregation_rounding_quantity','minimum_amount'}
    rules={}
    for category,raw in snapshot.get('billing_rules',{}).items():
        if category=='fc_cu_conversion' or not isinstance(raw,dict):continue
        values={key:Decimal(str(value)) if key in numeric and value is not None else value for key,value in raw.items() if key in supported}
        if values:rules[category]=BillingRule(**values)
    return rules

def _bind_billing_policy(snapshot,candidate_specs):
    if any(_value(candidate,'provider','') in {'ali_fc','ali_agentrun'} for candidate in candidate_specs):
        from atsuite.market_data.usage import FC_CU_RULE
        policies=snapshot.setdefault('billing_rules',{})
        policies.setdefault('fc_cu_conversion',dict(FC_CU_RULE))
        aggregate_rule=dict(FC_AGGREGATE_RULE)
        for card in snapshot.get('rate_cards',[]):
            if card.get('source') == 'aliyun_fc_public_tariff':
                # The mainland BSS catalog uses CNY, including the hourly floor.
                aggregate_rule.update(card['metadata']['billing_rule'])
        policies.setdefault('compute_units',aggregate_rule)

def _value(obj,name,default=None):
    return obj.get(name,default) if isinstance(obj,dict) else getattr(obj,name,default)

def _serializable(card):
    row=asdict(card)
    row['tiers']=[{'up_to':str(t.up_to) if t.up_to is not None else None,'unit_price':str(t.unit_price)} for t in card.tiers]
    return row


def _price_content(card):
    # Snapshot identity is content-derived; fetch timestamps vary per query and
    # must not turn an identical price card into an immutable-id collision.
    row=_serializable(card)
    row.pop('fetched_at',None)
    row.pop('observed_at',None)
    return row

class OnlineMarketData:
    def __init__(self, *, aws=None,gcp=None,ali=None,fx_provider=None):
        self.aws=aws;self.gcp=gcp;self.ali=ali
        self.fx_provider=fx_provider or ECBFxProvider()
    def fetch_candidate(self,candidate):
        provider=_value(candidate,'provider','')
        selector=SERVICE_SELECTORS.get(provider)
        if selector is None:
            raise ValueError('unsupported_price_service:'+provider)
        region=_value(candidate,'region','')
        if selector['vendor']=='aws':
            adapter=self.aws or AWSPriceAdapter()
            services=[]
            for page in adapter.client.get_paginator('describe_services').paginate(FormatVersion='aws_v1'):
                services.extend(page.get('Services',[]))
            matches=[s['ServiceCode'] for s in services if str(s.get('ServiceCode','')).lower() in selector['service_aliases']]
            if len(matches)!=1:
                raise ValueError('ambiguous_or_missing_price_service:'+provider)
            cards=adapter.fetch(matches[0],[{'Type':'TERM_MATCH','Field':'regionCode','Value':region}],region=region)
        elif selector['vendor']=='gcp':
            adapter=self.gcp or GCPCatalogAdapter()
            service_id=adapter.discover_service(selector['service_aliases'])
            cards=adapter.fetch(service_id,region=region)
        else:
            adapter=self.ali or AlibabaPriceAdapter()
            cards=adapter.fetch_candidate(candidate,service_aliases=selector['service_aliases'])
        if not cards:
            raise ValueError('missing_price:'+provider)
        # Preserve upstream SKU/unit/mode identities; never manufacture selected
        # usage selectors from requested resources. Collectors resolve meters.
        return cards

def freeze_market_data(candidate_specs, policy, approved_accounts=(), *, service=None, approved_snapshot=None):
    service=service or OnlineMarketData()
    policy_dict=policy if isinstance(policy,dict) else asdict(policy)
    mode=policy_dict.get('pricing','online')
    accounts=set(approved_accounts)
    for candidate in candidate_specs:
        account=_value(candidate,'account_id','')
        if account and account not in accounts:
            raise ValueError('market_account_not_approved')
    if mode in {'snapshot','offline'}:
        if approved_snapshot is None:
            raise ValueError('approved_market_snapshot_required')
        # Validate freshness at use, retaining gaps rather than rewriting labels.
        frozen=json.loads(json.dumps(approved_snapshot))
        frozen['freshness_checks']={c['snapshot_id']:freshness_at(c.get('observed_at',''),c.get('fetched_at',''),c.get('freshness','unknown')) for c in frozen.get('rate_cards',[])}
        stale=any(v!='fresh' for v in frozen['freshness_checks'].values())
        fx=frozen.get('fx') or frozen.get('fx_snapshot')
        if isinstance(fx,dict):
            fx_status=freshness_at(fx.get('observed_at',''),fx.get('fetched_at',''),fx.get('freshness','unknown'))
            frozen['fx_freshness']=fx_status
            stale=stale or fx_status!='fresh'
        if stale and policy_dict.get('stale_policy') != 'allow':
            raise ValueError('stale_market_snapshot_requires_explicit_acceptance')
        frozen['parent_snapshot_id']=frozen.pop('snapshot_id','')
        _bind_billing_policy(frozen,candidate_specs)
        frozen['status']='frozen_stale_approved' if stale else 'frozen'
        frozen['snapshot_id']='market-'+digest(frozen,length=32)
        return frozen
    cards,coverage={},[]
    for candidate in candidate_specs:
        cid=_value(candidate,'candidate_id',_value(candidate,'id',''))
        provider=_value(candidate,'provider','')
        try:
            rows=service.fetch_candidate(candidate)
            for card in rows:
                existing=cards.get(card.snapshot_id)
                if existing is not None and _price_content(existing)!=_price_content(card):
                    raise ValueError('immutable_price_snapshot_collision')
                cards.setdefault(card.snapshot_id,card)
            coverage.append({'candidate_id':cid,'provider':provider,'status':'catalog_fetched','snapshot_ids':[c.snapshot_id for c in rows],'required_meters':list(SERVICE_SELECTORS.get(provider,{}).get('meters',())),'usage_match_status':'pending_usage_ledger'})
        except Exception as exc:
            coverage.append({'candidate_id':cid,'provider':provider,'status':'missing','reason_code':str(exc) if isinstance(exc,ValueError) else type(exc).__name__})
    try:
        fx=service.fx_provider.fetch('USD').to_dict()
    except Exception as exc:
        fx=None
        coverage.append({'category':'fx','status':'missing','reason_code':type(exc).__name__})
    content={'schema_version':2,'rate_cards':[_serializable(c) for c in cards.values()],'fx':fx,'coverage':coverage,'source_policy':policy_dict}
    _bind_billing_policy(content,candidate_specs)
    content['snapshot_id']='market-'+digest(content,length=32)
    content['status']='frozen_partial' if any(c['status']=='missing' for c in coverage) else 'frozen'
    return content

def price_candidate_ledger(trials, market_snapshot, *, phase='confirmation', target_currency='USD', rules=None, required_categories=None, account_usage=None):
    """Derive candidate runtime cost while retaining every failed formal attempt.

    trials can be a UsageLedger or collected trial dicts with usage_entries/usage.
    No missing quantity is manufactured. Separate experiment spend keeps other
    stages; production cost uses the explicit confirmation phase only.
    """
    from atsuite.market_data.model import UsageEntry,UsageLedger
    from atsuite.market_data.reprice import load_rate_cards
    from atsuite.market_data.pricing import estimate_cost,cost_per_successful_workflow
    from dataclasses import fields
    entries=[]; success={}; candidates=set(); gaps=[]
    if isinstance(trials,UsageLedger):
        entries=list(trials.entries); candidates={e.candidate_id for e in entries}
    else:
        allowed={f.name for f in fields(UsageEntry)}
        for trial in trials:
            if 'quantity' in trial and 'category' in trial:
                values={k:v for k,v in trial.items() if k in allowed}
                values['quantity']=Decimal(str(values['quantity']))
                entry=UsageEntry(**values);entries.append(entry);candidates.add(entry.candidate_id)
                continue
            cid=str(trial.get('candidate_id') or '')
            candidates.add(cid)
            trial_phase=trial.get('phase','confirmation')
            if trial_phase==phase and trial.get('success') is True and trial.get('correctness_status') in {'passed','verified'}:
                success[cid]=success.get(cid,0)+1
            usage=trial.get('usage_entries',trial.get('usage',[]))
            if not usage:
                gaps.append({'candidate_id':cid,'run_id':trial.get('run_id',''),'reason_code':'missing_usage_attempt','phase':trial_phase})
            for raw in usage:
                if isinstance(raw,UsageEntry):
                    entries.append(replace(raw,phase=trial_phase)); continue
                values={k:v for k,v in raw.items() if k in allowed}
                values.setdefault('candidate_id',cid)
                values.setdefault('run_id',str(trial.get('run_id','')))
                values.setdefault('phase',trial_phase)
                if 'quantity' not in values:
                    gaps.append({'candidate_id':cid,'reason_code':'missing_usage_quantity','phase':trial_phase});continue
                values['quantity']=Decimal(str(values['quantity']))
                entries.append(UsageEntry(**values))
    ledger=UsageLedger(tuple(entries))
    rules=rules if rules is not None else frozen_billing_rules(market_snapshot)
    cards=load_rate_cards(market_snapshot)
    fx_raw=market_snapshot.get('fx') or market_snapshot.get('fx_snapshot')
    from atsuite.market_data.model import FxSnapshot
    fx=FxSnapshot(**fx_raw) if isinstance(fx_raw,dict) else load_fx_snapshot(fx_raw) if isinstance(fx_raw,str) else None
    formal=UsageLedger(tuple(entry for entry in ledger.entries if entry.phase==phase))
    experiment_spend=estimate_cost(ledger,cards,fx=fx,target_currency=target_currency,rules=rules,scope='experiment_spend_estimate',account_usage=account_usage)
    estimates=[replace(c,scope='declared_runtime_scope') for c in experiment_spend if c.phase==phase]
    results=[]
    for cid in sorted(candidates):
        rows=[c for c in estimates if c.candidate_id==cid]
        present={entry.category for entry in formal.entries if entry.candidate_id==cid}
        expected=set((required_categories or {}).get(cid,()))
        candidate_gaps=[g for g in gaps if g['candidate_id']==cid and g.get('phase',phase)==phase]
        candidate_gaps += [{'candidate_id':cid,'category':c,'reason_code':'missing_usage_category'} for c in sorted(expected-present)]
        complete=bool(rows) and not candidate_gaps and all(c.coverage=='available' and c.amount is not None for c in rows)
        total=sum((c.amount for c in rows),Decimal('0')) if complete else None
        results.append({'candidate_id':cid,'amount':str(total) if total is not None else None,'cost_per_successful_workflow':str(total/Decimal(success[cid])) if total is not None and success.get(cid,0)>0 else None,'successful_workflows':success.get(cid,0),'currency':target_currency,'cost_scope':'declared_runtime_scope','cost_basis':'rate_card_estimate','coverage':'available' if complete else 'missing','coverage_gaps':candidate_gaps+[{'category':c.category,'reason_code':c.reason_code or c.coverage} for c in rows if c.coverage!='available'],'rate_snapshot_ids':sorted({c.source_snapshot_id for c in rows}),'fx_snapshot_id':fx.snapshot_id if fx else '', 'ledger_digest':ledger.content_digest(),'phase':phase})
    trial_costs={}
    for item in estimates:
        if item.run_id not in trial_costs:
            trial_costs[item.run_id]=Decimal('0')
        if item.amount is None or item.coverage!='available':
            trial_costs[item.run_id]=None
        elif trial_costs[item.run_id] is not None:
            trial_costs[item.run_id]+=item.amount
    trial_costs={key:str(value) if value is not None else None for key,value in trial_costs.items()}
    singleton=results[0] if len(results)==1 else {}
    return {**singleton,'trial_costs':trial_costs,'coverage':'complete' if singleton.get('coverage')=='available' else 'missing','candidate_costs':results,'costs':[{**asdict(c),'amount':str(c.amount) if c.amount is not None else None,'quantity':str(c.quantity)} for c in estimates],'experiment_spend':[{**asdict(c),'amount':str(c.amount) if c.amount is not None else None,'quantity':str(c.quantity)} for c in experiment_spend],'usage_ledger_digest':ledger.content_digest()}


# Public adapter imports are kept here for a single built-in integration surface.
def usage_from_report(*args,**kwargs):
    from atsuite.market_data.usage import usage_from_report as adapt
    value=adapt(*args,**kwargs)
    return {**value,"entries":value["usage"],"coverage":value["usage_coverage"]}

def conservative_operation_estimates(candidates,market,protocol):
    from atsuite.market_data.usage import conservative_candidate_spend
    # Ancillary stages need their own frozen bounded metering assumptions. They
    # are never substituted by old Analyzer static constants or monetary zero.
    declared=market.get('operation_cost_bounds',{})
    estimates={stage:declared.get(stage) for stage in ('build','deploy','invoke','cleanup')}
    if estimates['invoke'] is None:
        bounds=[]
        for candidate in candidates:
            resources=_value(candidate,'resources_by_target',{})
            timeouts=[resource.get('timeout_seconds',resource.get('timeout')) for resource in resources.values()]
            timeout=max(timeouts) if timeouts and all(value is not None for value in timeouts) else None
            bound=conservative_candidate_spend(candidate,market,maximum_duration_seconds=timeout,maximum_calls=market.get('maximum_tool_calls'),session_idle_seconds=market.get('maximum_session_idle_seconds'),cleanup_reserve=declared.get('cleanup'))
            if bound.get('amount') is None:
                bounds=[];break
            bounds.append(Decimal(bound['amount']))
        if bounds:estimates['invoke']=str(max(bounds))
    for stage,value in estimates.items():
        if value is not None:
            decimal=Decimal(str(value))
            if not decimal.is_finite() or decimal < 0:
                raise ValueError('invalid_operation_cost_bound:'+stage)
            estimates[stage]=str(decimal)
    return estimates
