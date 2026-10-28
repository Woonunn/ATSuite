"""Selection semantics for paper figures; fixture budgets are not actual cloud fees."""
import copy
import unittest

from experiments.exp1.analysis.plot_e1a_selection import selection_data, frontier


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.reports=[];self.groups={}
        for role,n in [('low',53),('typical',191),('high',478)]:
            for p,latency,cost in [('aws_lambda',18,1),('aws_agentcore',16,4),('ali_fc',19,2),('ali_agentrun',20,3)]:
                if role=='high' and p=='aws_agentcore':latency=28
                if role=='high' and p=='ali_fc':cost=.5
                r={'provider':p,'sequence_seed':11,'window_role':role,'request_table_sha256':'paired','pilot':False,
                   'planned_requests_sha256':role,'window_duration_ms':600000,'slo_ms':120000,
                   'requests':n,'successes':n,'success_rate':1,'slo_attainment':1,'arrival_rate_per_s':n/600,
                   'latency_ms_success_only':{'p95':latency*1000},'total_cost_cny':None,'cost_status':'pending_vendor_billing'}
                if role=='high' and p=='ali_agentrun':r.update(successes=340,success_rate=340/478,slo_attainment=340/478)
                self.reports.append(r);self.groups[f'11:{role}:{p}']={'approved':True,'currency':'CNY','upper_cost_reservation':str(cost*n)}
        self.admission={'basis':'planning_admission_not_actual_billing','request_table_sha256':'paired','groups':self.groups}

    def test_measured_and_forecast_choices_stay_separate_and_429_failures_count(self):
        rows,choices=selection_data(self.reports,self.admission,6)
        self.assertEqual(choices['low']['observed_p95_minimum'],'aws_agentcore')
        self.assertEqual(choices['high']['observed_p95_minimum'],'aws_lambda')
        self.assertEqual(choices['low']['minimum_budget_forecast'],'aws_lambda')
        self.assertEqual(choices['high']['minimum_budget_forecast'],'ali_fc')
        self.assertTrue(all(c['actual_cost_minimum'] is None for c in choices.values()))
        failed=next(r for r in rows if r['role']=='high' and r['provider']=='ali_agentrun')
        self.assertFalse(failed['eligible']);self.assertAlmostEqual(failed['budget_cny_per_1000_successes'],1434*1000/340)
        front=frontier([r for r in rows if r['role']=='high'])
        self.assertEqual({r['provider'] for r in front},{'aws_lambda','ali_fc'})

    def test_incomplete_pairing_or_mixed_sources_fail_closed(self):
        with self.assertRaises(ValueError):selection_data(self.reports[:-1],self.admission,6)
        changed=copy.deepcopy(self.admission);changed['request_table_sha256']='other'
        with self.assertRaises(ValueError):selection_data(self.reports,changed,6)
        changed=copy.deepcopy(self.admission);changed['groups']['11:low:aws_lambda']['upper_cost_reservation']='NaN'
        with self.assertRaises(ValueError):selection_data(self.reports,changed,6)


if __name__=='__main__':unittest.main()
