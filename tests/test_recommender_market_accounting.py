import unittest
from decimal import Decimal
from atsuite.market_data.model import PriceTier, RateCard, UsageEntry, UsageLedger
from atsuite.market_data.pricing import BillingRule, estimate_cost, projected_monthly_cost
from atsuite.market_data.reprice import load_rate_cards

class AccountingRegressions(unittest.TestCase):
    def card(self, **kwargs):
        args = dict(snapshot_id='price', provider='aws', service='duration', sku='meter', region='r', billing_mode='on_demand', unit='s', currency='USD', tiers=(PriceTier(None, Decimal('1')),), fetched_at='', observed_at='', source='fixture')
        args.update(kwargs)
        return RateCard(**args)

    def test_each_attempt_rounds_and_empty_refs_do_not_dedupe(self):
        ledger = UsageLedger(tuple(UsageEntry('c', 'r', 'duration', Decimal('.001'), 's', 'aws') for _ in range(2)))
        cost = estimate_cost(ledger, {('aws','duration'):self.card()}, rules={'duration':BillingRule(minimum_quantity=Decimal('.1'))})
        self.assertEqual(sum(c.amount for c in cost), Decimal('.2'))

    def test_candidates_are_separate(self):
        ledger = UsageLedger(tuple(UsageEntry(c, 'r', 'duration', Decimal('1'), 's', 'aws') for c in ['a','b']))
        costs = estimate_cost(ledger, {('aws','duration'):self.card()})
        self.assertEqual(len(costs), 2)
        self.assertEqual({c.candidate_id for c in costs}, {'a','b'})

    def test_missing_is_not_zero(self):
        cost = estimate_cost(UsageLedger((UsageEntry('c','r','duration',Decimal('1'),'s','aws'),)), {})[0]
        self.assertIsNone(cost.amount)

    def test_loading_does_not_overwrite_regions(self):
        from dataclasses import asdict
        a, b = self.card(), self.card(region='other', snapshot_id='other')
        self.assertEqual(len(load_rate_cards({'rate_cards':[asdict(a),asdict(b)]})), 2)

    def test_monthly_total_requests_and_idle_units(self):
        self.assertEqual(projected_monthly_cost(Decimal('2'), 10, concurrency=3), Decimal('20'))
        with self.assertRaises(ValueError):
            projected_monthly_cost(Decimal('2'), 10, session_idle_seconds=Decimal('3'))

class FullAccountingContracts(AccountingRegressions):
    def test_region_selector_and_ambiguity(self):
        from dataclasses import replace
        ledger = UsageLedger((UsageEntry('c','r','duration',Decimal('1'),'s','aws',service='duration',region='r',sku='meter',billing_mode='on_demand'),))
        costs = estimate_cost(ledger,[self.card(),self.card(snapshot_id='other',region='other',tiers=(PriceTier(None,Decimal('7')),))])
        self.assertEqual(costs[0].amount,Decimal('1'))
        ambiguous = estimate_cost(ledger,[self.card(),self.card(snapshot_id='overlap')])
        self.assertEqual(ambiguous[0].reason_code,'ambiguous_price_match')
        self.assertIsNone(ambiguous[0].amount)

    def test_actual_meter_dedup_and_failures_kept(self):
        from dataclasses import replace
        first = UsageEntry('c','r','duration',Decimal('1'),'s','aws',provider_meter_id='request1',metadata={'status':'failed'})
        costs = estimate_cost(UsageLedger((first,first,replace(first,run_id='r2',provider_meter_id='request2'))),{('aws','duration'):self.card()})
        self.assertEqual(len(costs),2)
        self.assertEqual(sum(c.quantity for c in costs),Decimal('2'))
        self.assertEqual(sum(c.amount for c in costs),Decimal('2'))

    def test_account_tiers_do_not_reset_for_each_candidate(self):
        card = self.card(tiers=(PriceTier(Decimal('1'),Decimal('0')),PriceTier(None,Decimal('2'))))
        entries = tuple(UsageEntry(c,c,'duration',Decimal('1'),'s','aws',account_id='acct',billing_period='2026-10') for c in ('a','b'))
        costs = estimate_cost(UsageLedger(entries),{('aws','duration'):card})
        self.assertEqual([c.amount for c in costs],[Decimal('0'),Decimal('2')])

    def test_explicit_conversion(self):
        costs = estimate_cost(UsageLedger((UsageEntry('c','r','duration',Decimal('1000'),'ms','aws'),)),{('aws','duration'):self.card()})
        self.assertEqual(costs[0].amount,Decimal('1'))
        self.assertEqual(costs[0].unit,'s')

    def test_unit_is_part_of_unique_price_selector(self):
        entry=UsageEntry('c','r','duration',Decimal('1'),'s','aws',service='duration',sku='meter',region='r',billing_mode='on_demand')
        cost=estimate_cost(UsageLedger((entry,)),[self.card(),self.card(snapshot_id='request-card',unit='request')])[0]
        self.assertEqual(cost.amount,Decimal('1'))
        self.assertEqual(cost.source_snapshot_id,'price')

    def test_hourly_function_cu_rounding_and_minimum_attributed_once(self):
        card=self.card(unit='CU',tiers=(PriceTier(None,Decimal('.00002')),))
        entries=tuple(UsageEntry('c',str(i),'compute_units',Decimal('.1'),'CU','aws',resource_id='function',account_id='acct',usage_start='2026-10-08T01:02:00+00:00',billing_period='2026-10',phase='confirmation') for i in range(2))
        rule=BillingRule(aggregation_scope='resource_hour',aggregation_rounding_quantity=Decimal('1'),minimum_amount=Decimal('.01'),minimum_currency='USD')
        costs=estimate_cost(UsageLedger(entries),{('aws','compute_units'):card},rules={'compute_units':rule})
        self.assertEqual(sum(c.quantity for c in costs),Decimal('1'))
        self.assertEqual(sum(c.amount for c in costs),Decimal('.01'))
        self.assertEqual([c.amount for c in costs],[Decimal('.005'),Decimal('.005')])
        other=UsageEntry('d','d','compute_units',Decimal('.1'),'CU','aws',resource_id='other-function',account_id='acct',usage_start='2026-10-08T01:02:00+00:00',billing_period='2026-10',phase='confirmation')
        costs=estimate_cost(UsageLedger((*entries,other)),{('aws','compute_units'):card},rules={'compute_units':rule})
        self.assertEqual(sum(c.amount for c in costs),Decimal('.02'))

    def test_hourly_rule_missing_resource_interval_cannot_price(self):
        rule=BillingRule(aggregation_scope='resource_hour',aggregation_rounding_quantity=Decimal('1'),minimum_amount=Decimal('.01'),minimum_currency='USD')
        entry=UsageEntry('c','r','compute_units',Decimal('.1'),'CU','aws')
        cost=estimate_cost(UsageLedger((entry,)),{('aws','compute_units'):self.card(unit='CU')},rules={'compute_units':rule})[0]
        self.assertIsNone(cost.amount)
        self.assertEqual(cost.reason_code,'missing_billing_interval_or_resource')

    def test_stale_fx_is_checked_at_use(self):
        from atsuite.market_data.model import FxSnapshot
        fx = FxSnapshot('old','USD',{'CNY':'7'},'2000-01-01','2000-01-01','fixture',freshness='fresh')
        cost = estimate_cost(UsageLedger((UsageEntry('c','r','duration',Decimal('1'),'s','aws'),)),{('aws','duration'):self.card(currency='CNY')},fx=fx)[0]
        self.assertEqual(cost.coverage,'stale')
        self.assertEqual(cost.amount,Decimal('1')/Decimal('7'))

    def test_ecb_preserves_publication_date_and_rejects_entities(self):
        import io
        from atsuite.market_data.fx import ECBFxProvider
        xml = b'<Envelope><Cube><Cube time="2026-10-07"><Cube currency="USD" rate="1.1"/></Cube></Cube></Envelope>'
        snap = ECBFxProvider(opener=lambda *a,**k:io.BytesIO(xml)).fetch()
        self.assertEqual(snap.observed_at,'2026-10-07')
        self.assertEqual(snap.observed_precision,'date')
        with self.assertRaises(ValueError):
            ECBFxProvider(opener=lambda *a,**k:io.BytesIO(b'<!DOCTYPE foo><Envelope/>')).fetch()

    def test_price_content_changes_snapshot_identity(self):
        from atsuite.market_data.providers.aws import AWSPriceAdapter
        payload = {'product':{'sku':'x','attributes':{'regionCode':'r'}},'terms':{'OnDemand':{'t':{'effectiveDate':'2026-10-08','priceDimensions':{'meter':{'unit':'s','pricePerUnit':{'USD':'1'}}}}}}}
        adapter = AWSPriceAdapter(client=object())
        first = adapter.parse(payload)[0]
        payload['terms']['OnDemand']['t']['priceDimensions']['meter']['pricePerUnit']['USD']='2'
        self.assertNotEqual(first.snapshot_id,adapter.parse(payload)[0].snapshot_id)

    def test_reprice_callback_versions_candidates_immutably(self):
        import tempfile,json,copy
        from pathlib import Path
        from dataclasses import asdict
        from atsuite.market_data.reprice import reprice
        old = {'objective':{'type':'min_cost'},'candidates':[{'candidate_id':'a'},{'candidate_id':'b'}]}
        frozen = copy.deepcopy(old)
        calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.jsonl'
            path.write_text('\n'.join(json.dumps({'candidate_id':c,'run_id':c,'category':'duration','quantity':'1','unit':'s','provider':'aws'}) for c in ('a','b')))
            card = asdict(self.card());card['tiers']=[{'up_to':None,'unit_price':'1'}]
            result=reprice(old,{'rate_cards':[card]},usage_ledger_path=path,comparison=lambda experiment,costs,snapshot:calls.append(costs) or {'status':'simulated_contract'})
        self.assertEqual(old,frozen)
        self.assertEqual(len(calls[0]),2)
        self.assertEqual(result['recommendation']['status'],'simulated_contract')
        self.assertTrue(result['parent_report_digest'])

class DefaultMarketServiceContracts(unittest.TestCase):
    def test_stale_snapshot_acceptance_uses_frozen_typed_policy(self):
        from atsuite.market_data.service import freeze_market_data
        from atsuite.recommender.contracts import MarketDataPolicy
        card=self.card()
        card.update(observed_at='2000-01-01',fetched_at='2000-01-01')
        snapshot={'snapshot_id':'original','rate_cards':[card]}
        for policy in ('reject','require_explicit_acceptance'):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError,'stale_market_snapshot'):
                freeze_market_data([],MarketDataPolicy(pricing='snapshot',stale_policy=policy),approved_snapshot=snapshot)
        allowed=freeze_market_data([],MarketDataPolicy(pricing='snapshot',stale_policy='allow'),approved_snapshot=snapshot)
        self.assertEqual(allowed['status'],'frozen_stale_approved')
        self.assertEqual(allowed['freshness_checks'][card['snapshot_id']],'stale')
        self.assertEqual(snapshot['snapshot_id'],'original')
        self.assertNotEqual(allowed['snapshot_id'],snapshot['snapshot_id'])
    def card(self,provider='aws',category='duration',region='us-east-1',unit='GB-s'):
        from dataclasses import asdict
        from datetime import datetime,timezone
        now=datetime.now(timezone.utc).isoformat()
        c=RateCard(provider+'-rate-'+category,provider,'service',category,region,'on_demand',unit,'USD',(PriceTier(None,Decimal('.1')),),now,now,'external-fixture',metadata={'category':category})
        row=asdict(c);row['tiers']=[{'up_to':None,'unit_price':'.1'}]
        return row
    def test_default_freeze_contract_covers_six_runtime_selectors(self):
        from atsuite.market_data.service import freeze_market_data,SERVICE_SELECTORS
        from atsuite.market_data.fx import StaticFxProvider
        from types import SimpleNamespace
        calls=[]
        from atsuite.market_data.reprice import _rate_card
        vendor_cards={v:_rate_card(self.card(provider=v)) for v in {'aws','ali','gcp'}}
        cards={p:vendor_cards[v['vendor']] for p,v in SERVICE_SELECTORS.items()}
        service=SimpleNamespace(fx_provider=StaticFxProvider({}),fetch_candidate=lambda c:calls.append(c['provider']) or [cards[c['provider']]])
        candidates=[{'candidate_id':p,'provider':p,'region':'us-east-1'} for p in SERVICE_SELECTORS]
        frozen=freeze_market_data(candidates,{'pricing':'online'},service=service)
        self.assertEqual(set(calls),set(SERVICE_SELECTORS))
        self.assertEqual(len(frozen['coverage']),6)
        self.assertEqual(frozen['status'],'frozen')
        self.assertTrue(all(c['usage_match_status']=='pending_usage_ledger' for c in frozen['coverage']))
    def test_online_failure_never_static_fallback(self):
        from atsuite.market_data.service import freeze_market_data
        from atsuite.market_data.fx import StaticFxProvider
        from types import SimpleNamespace
        def failure(candidate):raise ValueError('missing_price')
        result=freeze_market_data([{'candidate_id':'a','provider':'aws_lambda'}],{'pricing':'online'},service=SimpleNamespace(fetch_candidate=failure,fx_provider=StaticFxProvider({})))
        self.assertEqual(result['status'],'frozen_partial')
        self.assertEqual(result['rate_cards'],[])
        self.assertEqual(result['coverage'][0]['reason_code'],'missing_price')
    def test_analyzer_usage_uses_actual_aws_billing_not_defaults(self):
        from atsuite.market_data.service import usage_from_report,price_candidate_ledger
        snapshot={'rate_cards':[self.card(),self.card(category='request',unit='request')]}
        candidate={'candidate_id':'c','provider':'aws_lambda','region':'us-east-1'}
        assignment={'run_id':'run','phase':'confirmation','tenant_id':'tenant','experiment_id':'experiment','attempt_id':'attempt'}
        metric={'metric_id':'metric','provider':'aws_lambda','source':'cloudwatch','provider_request_id':'rid','invocation_call_id':'call','evidence_refs':['provider-log'],'fields':{'billed_duration_ms':100,'memory_limit_mb':1024,'observed_fields':['billed_duration_ms','memory_limit_mb']}}
        report={'run':{'uid':'run','start_time':1791374400,'end_time':1791374401},'provider_metrics':[metric],'invocations':[{'call_id':'call','target_id':'target','provider_request_id':'rid','status':'error'}]}
        usage=usage_from_report(report,candidate,assignment,'acct',market_snapshot=snapshot)
        self.assertEqual(usage['coverage'],[])
        self.assertEqual(len(usage['entries']),2)
        self.assertTrue(all(entry['tenant_id']=='tenant' and entry['experiment_id']=='experiment' and entry['attempt_id']=='attempt' for entry in usage['entries']))
        self.assertTrue(all(entry['usage_start'] and entry['usage_end'] and entry['billing_period']=='2026-10' for entry in usage['entries']))
        cost=price_candidate_ledger(usage['entries'],snapshot)
        self.assertEqual(cost['coverage'],'complete')
        self.assertEqual(Decimal(cost['amount']),Decimal('.11'))
        metric['fields']['observed_fields']=[]
        missing=usage_from_report(report,candidate,assignment,'acct',market_snapshot=snapshot)
        self.assertTrue(any(g['reason_code']=='missing_billed_duration_or_memory' for g in missing['coverage']))
    def test_confirmation_scope_does_not_bill_warmup_as_production(self):
        from atsuite.market_data.service import price_candidate_ledger
        entries=[{'candidate_id':'c','run_id':phase,'category':'duration','quantity':'1','unit':'GB-s','provider':'aws','phase':phase} for phase in ('warmup','exploration','confirmation')]
        result=price_candidate_ledger(entries,{'rate_cards':[self.card()]})
        self.assertEqual(Decimal(result['amount']),Decimal('.1'))
        self.assertEqual(len(result['experiment_spend']),3)
    def test_unknown_bounds_prevent_paid_dispatch(self):
        from atsuite.market_data.service import conservative_operation_estimates
        self.assertEqual(conservative_operation_estimates([],{},{}),{'build':None,'deploy':None,'invoke':None,'cleanup':None})
        result=conservative_operation_estimates([],{'operation_cost_bounds':{'build':'1','deploy':'2','invoke':'3','cleanup':'4'}},{})
        self.assertEqual(result,{'build':'1','deploy':'2','invoke':'3','cleanup':'4'})

    def test_ali_frozen_policy_is_bound_and_used_by_default_service(self):
        from atsuite.market_data.service import freeze_market_data,price_candidate_ledger
        from datetime import datetime,timezone
        now=datetime.now(timezone.utc).isoformat();hour=now[:13]+':02:00+00:00'
        candidate={'candidate_id':'c','provider':'ali_fc','region':'r'}
        card=self.card(provider='ali',category='compute_units',region='r',unit='CU');card.update(service='fc',tiers=[{'up_to':None,'unit_price':'.00002'}])
        frozen=freeze_market_data([candidate],{'pricing':'snapshot'},approved_snapshot={'rate_cards':[card]})
        self.assertEqual(frozen['billing_rules']['compute_units']['minimum_amount'],'.01')
        entry={'candidate_id':'c','run_id':'r','category':'compute_units','quantity':'.1','unit':'CU','provider':'ali','service':'fc','region':'r','sku':'compute_units','billing_mode':'on_demand','account_id':'acct','resource_id':'f','usage_start':hour,'billing_period':now[:7],'phase':'confirmation'}
        result=price_candidate_ledger([entry],frozen)
        self.assertEqual(result['amount'],'0.01000')
        self.assertEqual(result['coverage'],'complete')

class CostUncertaintyContracts(unittest.TestCase):
    def test_all_attempt_costs_and_success_denominator(self):
        from atsuite.market_data.statistics import cost_interval
        trials=[{'run_id':str(i),'phase':'confirmation','block_id':str(i),'status':'success' if i<9 else 'failed','output_validation':'passed' if i<9 else 'unknown'} for i in range(10)]
        interval=cost_interval(trials,{str(i):'1' for i in range(10)},resamples=1000)
        self.assertAlmostEqual(interval.estimate,10/9)
        self.assertGreater(interval.upper,interval.lower)
        self.assertTrue(interval.method.endswith('_v1'))
    def test_zero_success_bootstrap_draws_stay_unbounded(self):
        from atsuite.market_data.statistics import cost_interval
        trials=[{'run_id':str(i),'phase':'confirmation','block_id':str(i),'status':'success' if i==0 else 'failed','output_validation':'passed' if i==0 else 'unknown'} for i in range(3)]
        interval=cost_interval(trials,{str(i):'1' for i in range(3)})
        self.assertEqual(interval.estimate,3)
        self.assertIsNone(interval.upper)
    def test_unknown_cost_is_not_zero_in_bootstrap(self):
        from atsuite.market_data.statistics import cost_interval
        trial={'run_id':'r','phase':'confirmation','block_id':'b','status':'success','output_validation':'passed'}
        interval=cost_interval([trial],{})
        self.assertIsNone(interval.lower);self.assertIsNone(interval.upper)
        self.assertEqual(interval.method,'unavailable_usage_lineage')
