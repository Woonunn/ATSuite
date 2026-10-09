"""Provider-observation ledger adapter; requested defaults are never observations."""
from __future__ import annotations
from dataclasses import asdict
from decimal import Decimal, InvalidOperation,ROUND_UP
from datetime import datetime,timezone
from atsuite.market_data.model import UsageEntry
from atsuite.market_data.reprice import load_rate_cards
from atsuite.market_data.service import SERVICE_SELECTORS

# Frozen official resource-to-CU rules, not monetary prices. Live prices still
# come from catalog snapshots. Users can freeze a newer explicit rule version.
FC_CU_RULE = {'version':'2026-08-25','source_url':'https://www.alibabacloud.com/help/en/functioncompute/pay-as-you-go-billing-methods','cpu_per_vcpu_second':'1','memory_per_gb_second':'.15','request_per_invocation':'.0075','disk_per_gb_second':'.05','free_active_disk_gb':'10','hourly_minimum_usd':'.01'}

def _get(obj,name,default=None):
    return obj.get(name,default) if isinstance(obj,dict) else getattr(obj,name,default)

def _timestamp(value):
    if value is None or isinstance(value,bool):return ''
    if isinstance(value,(int,float)):
        return datetime.fromtimestamp(value,timezone.utc).isoformat() if value>0 else ''
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).isoformat()
    except ValueError:return ''

def _decimal(fields,name,observed):
    if name not in observed or fields.get(name) is None or isinstance(fields[name],bool):
        return None
    try:
        value=Decimal(str(fields[name]))
        return value if value.is_finite() and value>=0 else None
    except InvalidOperation:
        return None

def _category(card):
    explicit=card.metadata.get('category') or card.metadata.get('meter_category')
    if explicit:return str(explicit)
    text=' '.join([card.service,card.sku,str(card.metadata.get('description','')),str(card.metadata.get('descriptions','')),str(card.metadata.get('attributes',{}))]).lower()
    if any(v in text for v in ('network','transfer','egress')):return 'network'
    if any(v in text for v in ('ephemeral','storage','disk')):return 'storage'
    if any(v in text for v in ('request','invocation')):return 'request'
    if any(v in text for v in ('compute unit','cu usage','computeunits')) or card.unit.lower()=='cu':return 'compute_units'
    if any(v in text for v in ('memory','ram')):return 'memory'
    if any(v in text for v in ('vcpu','cpu')):return 'cpu'
    if 'duration' in text:return 'duration'
    return 'unknown'

def _match(cards,provider,region,category,architecture='',mode=''):
    vendor=SERVICE_SELECTORS.get(provider,{}).get('vendor',provider)
    matches=[]
    for card in cards:
        if card.provider not in {provider,vendor}:continue
        if card.region!=region and not (card.region in {'global','multi-region'} and region in card.metadata.get('regions',[])):continue
        card_category=_category(card)
        if card_category!=category:continue
        text=str(card.metadata).lower()
        if architecture and ('arm' in text) != ('arm' in architecture.lower()) and ('arm' in text or 'x86' in text):continue
        if mode and card.metadata.get('billing_mode') and card.metadata['billing_mode']!=mode:continue
        matches.append(card)
    if len(matches)!=1:
        raise ValueError('ambiguous_price_match' if matches else 'missing_price')
    return matches[0]

def _in_unit(quantity,unit,card_unit):
    aliases={'GB-Seconds':'GB-s','GB-Second':'GB-s','GiBy.s':'GB-s','GiB-s':'GB-s','GB-Hours':'GB-h','GB-Hour':'GB-h','vCPU-Hours':'vCPU-h','vCPU-Hour':'vCPU-h','CPU-Hours':'vCPU-h','Requests':'request','requests':'request','count':'request','CU':'CU','unit':'unit'}
    target=aliases.get(card_unit,card_unit)
    source=aliases.get(unit,unit)
    if source==target:return quantity
    for left,right,factor in (('GB-h','GB-s',Decimal('3600')),('vCPU-h','vCPU-s',Decimal('3600')),('vCPU-h','s',Decimal('3600')),('vCPU-s','s',Decimal('1')),('ms','s',Decimal('.001'))):
        if source==left and target==right:return quantity*factor
        if source==right and target==left:return quantity/factor
    raise ValueError('incompatible_usage_unit')

def usage_from_report(report_dict,candidate,assignment,account_id,*,market_snapshot,phase=None):
    cards=load_rate_cards(market_snapshot)
    cid=_get(candidate,'candidate_id','')
    provider=_get(candidate,'provider','')
    region=_get(candidate,'region','')
    arch=_get(candidate,'runtime_architecture','')
    run=report_dict.get('run',{})
    usage_start=_timestamp(run.get('start_time'))
    usage_end=_timestamp(run.get('end_time'))
    run_id=str(_get(assignment,'run_id',run.get('uid','')))
    phase=phase or _get(assignment,'phase','confirmation')
    entries=[];gaps=[]
    metrics=report_dict.get('provider_metrics',[])
    invocations={row.get('call_id'):row for row in report_dict.get('invocations',[])}
    covered_calls=set()
    for metric in metrics:
        f=metric.get('fields') or {}
        observed=set(f.get('observed_fields',[]))
        source=metric.get('source','')
        refs=metric.get('evidence_refs') or []
        call_id=metric.get('invocation_call_id','')
        invocation=invocations.get(call_id,{})
        target=invocation.get('target_id',str(f.get('runtime_name','')))
        resource_id=(_get(candidate,'resource_ids_by_target',{}) or {}).get(target) or f.get('resource_id') or f.get('function_name') or (cid+':'+str(target) if target else '')
        request_id=metric.get('provider_request_id') or invocation.get('provider_request_id') or ''
        session_id=f.get('session_id') or invocation.get('provider_session_id') or ''
        if not refs:
            gaps.append({'category':'usage','reason_code':'missing_provider_usage_evidence','metric_id':metric.get('metric_id','')});continue
        quantities=[]
        if provider=='aws_agentcore':
            if source!='agentcore_usage':continue
            # Aggregate session meters once. Require logs and actual quantities,
            # never per-invocation client time or tool duration.
            if not f.get('log_entries') or not session_id:
                gaps.append({'category':'session','reason_code':'missing_session_usage'});continue
            if not observed:observed={key for key in ('vcpu_hours','memory_gb_hours') if key in f}
            for field,category,unit in (('vcpu_hours','cpu','vCPU-h'),('memory_gb_hours','memory','GB-h')):
                quantity=_decimal(f,field,observed)
                if quantity is None:gaps.append({'category':category,'reason_code':'missing_usage_quantity'})
                else:quantities.append((category,quantity,unit))
        elif provider=='aws_lambda':
            duration=_decimal(f,'billed_duration_ms',observed)
            memory=_decimal(f,'memory_limit_mb',observed)
            if duration is None or memory is None:
                gaps.append({'category':'duration','reason_code':'missing_billed_duration_or_memory','call_id':call_id})
            else:
                quantities.append(('duration',duration*memory/Decimal('1024000'),'GB-s'))
            if request_id:quantities.append(('request',Decimal('1'),'request'))
            else:gaps.append({'category':'request','reason_code':'missing_request_id'})
        elif provider in {'gcp_faas','gcp_mcp'}:
            duration=_decimal(f,'duration_ms',observed)
            cpu=_decimal(f,'cpu',observed)
            memory=_decimal(f,'memory',observed)
            if memory is None:memory=_decimal(f,'memory_mb',observed)
            if duration is None or cpu is None or memory is None:
                gaps.append({'category':'cpu_memory','reason_code':'missing_measured_resource_time'})
            else:
                quantities.extend([('cpu',duration*cpu/Decimal('1000'),'vCPU-s'),('memory',duration*memory/Decimal('1024000'),'GB-s')])
            if request_id:quantities.append(('request',Decimal('1'),'request'))
        elif provider in {'ali_fc','ali_agentrun'}:
            cu=_decimal(f,'compute_units',observed)
            if cu is not None:quantities.append(('compute_units',cu,'CU'))
            else:
                duration=_decimal(f,'duration_ms',observed)
                cpu=_decimal(f,'cpu',observed)
                memory=_decimal(f,'memory',observed)
                if duration is None or cpu is None or memory is None or not request_id:
                    gaps.append({'category':'compute_units','reason_code':'missing_measured_resource_time'})
                else:
                    rule=market_snapshot.get('billing_rules',{}).get('fc_cu_conversion',FC_CU_RULE)
                    quantity=duration/Decimal('1000')*(cpu*Decimal(rule['cpu_per_vcpu_second'])+memory/Decimal('1024')*Decimal(rule['memory_per_gb_second']))+Decimal(rule['request_per_invocation'])
                    quantities.append(('compute_units',quantity,'CU'))
        else:
            gaps.append({'category':'usage','reason_code':'unsupported_usage_service'})
        if quantities:covered_calls.add(call_id)
        for category,quantity,unit in quantities:
            try:
                card=_match(cards,provider,region,category,arch,_get(candidate,'billing_mode',''))
                quantity=_in_unit(quantity,unit,card.unit)
                entry=UsageEntry(cid,run_id,category,quantity,card.unit,card.provider,resource_id=str(resource_id),source_ref=str(refs[0]),metadata={'provider_metric_id':metric.get('metric_id',''),'evidence_refs':refs,'source':source,'observed_fields':sorted(observed),'usage_interval_source':'observed_run_envelope' if usage_start else 'missing'},tenant_id=str(_get(assignment,'tenant_id','')),experiment_id=str(_get(assignment,'experiment_id','')),attempt_id=str(_get(assignment,'attempt_id',run_id)),service=card.service,account_id=account_id,region=region,sku=card.sku,architecture=card.architecture,billing_mode=card.billing_mode,currency=card.currency,price_scope=card.price_scope,phase=phase,usage_start=usage_start,usage_end=usage_end,billing_period=str(_get(assignment,'billing_period',usage_start[:7])),provider_meter_id=str(session_id if provider=='aws_agentcore' else request_id))
                entries.append(entry)
            except ValueError as exc:gaps.append({'category':category,'reason_code':str(exc)})
    if not metrics:gaps.append({'category':'usage','reason_code':'missing_provider_metrics'})
    if provider!='aws_agentcore':
        for call_id in set(invocations)-covered_calls:
            gaps.append({'category':'usage','call_id':call_id,'reason_code':'unmatched_invocation_usage'})
    # State/network/idle billables must be explicit, even if execution meters exist.
    for expected in report_dict.get('required_usage_categories',[]):
        if expected not in {entry.category for entry in entries}:
            gaps.append({'category':expected,'reason_code':'missing_usage_category'})
    from atsuite.market_data.model import UsageLedger
    unique=UsageLedger(tuple(entries)).deduplicated()
    return {'usage':[{**asdict(e),'quantity':str(e.quantity)} for e in unique.entries],'usage_coverage':gaps,'status':'available' if unique.entries and not gaps else 'partial' if unique.entries else 'missing'}

def conservative_candidate_spend(candidate,market_snapshot,*,maximum_duration_seconds,maximum_calls,maximum_attempts=1,session_idle_seconds=None,cleanup_reserve=None):
    """Maximum executable runtime attribution from frozen selectors and limits.

    This is a dispatch budget bound, never measured candidate evidence. Unknown
    billable items and absent cleanup reserve prevent paid dispatch.
    """
    from atsuite.market_data.pricing import estimate_cost
    from atsuite.market_data.model import UsageLedger
    if maximum_duration_seconds is None or maximum_calls is None or maximum_attempts<1:
        return {'amount':None,'reason_code':'missing_execution_bound'}
    if cleanup_reserve is None:
        return {'amount':None,'reason_code':'missing_cleanup_reserve'}
    provider=_get(candidate,'provider','');region=_get(candidate,'region','');cid=_get(candidate,'candidate_id','')
    resource_map=_get(candidate,'resources_by_target',{})
    if not resource_map:
        return {'amount':None,'reason_code':'missing_resource_bound'}
    seconds=Decimal(str(maximum_duration_seconds));calls=Decimal(str(maximum_calls));attempts=Decimal(str(maximum_attempts))
    quantities=[]
    minimum_bound=Decimal('0')
    for target,resource in resource_map.items():
        memory=resource.get('memory_mb');cpu=resource.get('cpu')
        if provider=='aws_lambda' and memory is not None:
            quantities.extend([(target,'duration',seconds*Decimal(str(memory))/Decimal('1024')*calls*attempts,'GB-s'),(target,'request',calls*attempts,'request')])
        elif provider in {'aws_agentcore','gcp_faas','gcp_mcp'} and cpu is not None and memory is not None:
            if provider in {'aws_agentcore','gcp_mcp'} and session_idle_seconds is None:
                return {'amount':None,'reason_code':'missing_session_idle_bound'}
            active=seconds*calls*attempts
            total=active+Decimal(str(session_idle_seconds or 0))
            quantities.extend([(target,'cpu',active*Decimal(str(cpu)),'vCPU-s'),(target,'memory',total*Decimal(str(memory))/Decimal('1024'),'GB-s')])
            if provider.startswith('gcp'):quantities.append((target,'request',calls*attempts,'request'))
        elif provider in {'ali_fc','ali_agentrun'} and cpu is not None and memory is not None:
            if provider=='ali_agentrun' and session_idle_seconds is None:
                return {'amount':None,'reason_code':'missing_session_idle_bound'}
            rule=market_snapshot.get('billing_rules',{}).get('fc_cu_conversion',FC_CU_RULE)
            active=seconds*calls*attempts
            total=active+Decimal(str(session_idle_seconds or 0))
            cu=active*Decimal(str(cpu))*Decimal(rule['cpu_per_vcpu_second'])+total*Decimal(str(memory))/Decimal('1024')*Decimal(rule['memory_per_gb_second'])+calls*attempts*Decimal(rule['request_per_invocation'])
            # Every invocation may straddle an hourly boundary; bound both CU
            # rounding and the active-function hourly minimum conservatively.
            active_hours=(seconds/Decimal('3600')).to_integral_value(rounding=ROUND_UP)+1
            if session_idle_seconds:active_hours+=(Decimal(str(session_idle_seconds))/Decimal('3600')).to_integral_value(rounding=ROUND_UP)
            hours=active_hours*calls*attempts
            cu+=hours
            minimum_bound+=Decimal(str(rule['hourly_minimum_usd']))*hours
            quantities.append((target,'compute_units',cu,'CU'))
        else:return {'amount':None,'reason_code':'unsupported_execution_bound'}
    cards=load_rate_cards(market_snapshot);entries=[]
    try:
        for target,category,quantity,unit in quantities:
            card=_match(cards,provider,region,category,_get(candidate,'runtime_architecture',''))
            entries.append(UsageEntry(cid,'budget-bound',category,_in_unit(quantity,unit,card.unit),card.unit,card.provider,resource_id=target,service=card.service,region=region,sku=card.sku,billing_mode=card.billing_mode,currency=card.currency,price_scope=card.price_scope))
    except ValueError as exc:return {'amount':None,'reason_code':str(exc)}
    fx_raw=market_snapshot.get('fx') or market_snapshot.get('fx_snapshot')
    from atsuite.market_data.model import FxSnapshot
    fx=FxSnapshot(**fx_raw) if isinstance(fx_raw,dict) else None
    costs=estimate_cost(UsageLedger(tuple(entries)),cards,fx=fx,target_currency='USD')
    if any(c.amount is None or c.coverage!='available' for c in costs):
        return {'amount':None,'reason_code':'missing_or_stale_bound_price'}
    amount=max(sum((c.amount for c in costs),Decimal('0')),minimum_bound)+Decimal(str(cleanup_reserve))
    return {'amount':str(amount),'currency':'USD','cost_basis':'conservative_budget_bound','maximum_calls':maximum_calls,'maximum_attempts':maximum_attempts,'source_snapshot_ids':[c.source_snapshot_id for c in costs]}
