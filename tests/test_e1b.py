import copy
import csv
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
import asyncio
from experiments.exp1.runner import run_e1b_local
import yaml
from experiments.exp1.data import prepare_e1b as p
from experiments.exp1.analysis.compare_composition import analyze


class CompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config=yaml.safe_load((p.ROOT/'experiments/exp1/configs/e1b.yaml').read_text())

    def test_measured_overlapping_classes(self):
        tasks=json.loads((p.ROOT/p.PROFILE).read_text())['tasks']
        classes={t['task_id']:p.classify(t['profiling'],self.config['thresholds']) for t in tasks}
        self.assertEqual(classes['human_142'],['CPU-intensive'])
        self.assertEqual(len(classes['human_12']),3)
        self.assertEqual(classes['human_15'],['Relatively lightweight'])
        with self.assertRaises(ValueError):p.classify({},self.config['thresholds'])
        bad=copy.deepcopy(tasks[0]['profiling']);bad['peak_rss_bytes_max']=float('nan')
        with self.assertRaises(ValueError):p.classify(bad,self.config['thresholds'])

    def test_quotas_reproduce_without_dropping(self):
        a=p.allocate({'a':.1,'b':.9},53,random.Random(11))
        self.assertEqual(a,p.allocate({'a':.1,'b':.9},53,random.Random(11)))
        self.assertEqual(a.count('a'),5);self.assertEqual(len(a),53)
        for weights in ({'a':-1},{'a':float('nan')},{'a':0}):
            with self.assertRaises(ValueError):p.allocate(weights,10,random.Random(11))

    def test_invalid_stages_and_timing(self):
        for key,value in [('time_scale',2),('stage_boundaries_ms',[0,400000,200000,600000])]:
            config={**self.config,key:value}
            with self.assertRaises(ValueError):p.prepare(config,'/must-not-create')

    def test_real_inputs_pairing_reproducibility_and_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            a=Path(temp)/'a';b=Path(temp)/'b'
            p.prepare(self.config,a);p.prepare(self.config,b)
            self.assertEqual((a/'requests.csv').read_bytes(),(b/'requests.csv').read_bytes())
            manifest,rows=p.load(a)
            self.assertEqual(len(rows),2166)
            for role in self.config['window_roles']:
                signatures=[]
                for scenario in self.config['scenarios']:
                    signatures.append([(r['arrival_id'],r['relative_arrival_ms']) for r in rows if r['scenario']==scenario and r['window_role']==role])
                self.assertTrue(all(s==signatures[0] for s in signatures))
            groups=[g for g in manifest['groups'] if g['window_role']=='typical' and g['scenario']=='cpu_ramp']
            ratios=[g['actual_task_counts'].get('human_142',0)/g['requests'] for g in groups]
            self.assertEqual(ratios,sorted(ratios));self.assertGreater(ratios[-1]-ratios[0],.6)
            with (a/'requests.csv').open('a') as f:f.write('corruption\n')
            with self.assertRaises(ValueError):p.load(a)

    def test_local_failure_propagates(self):
        row=dict(arrival_id='test',relative_arrival_ms='0',original_timestamp_utc='2026-06-06T00:00:00Z',source_partition='test',source_type='unit_fixture',window_id='test',scenario='fixed',window_role='low')
        manifest=dict(config=dict(window_duration_ms=100,queue_capacity=2),request_table_sha256='fixture')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'manifest.json').write_text('{}')
            with patch.object(run_e1b_local,'load',return_value=(manifest,[row])), patch.object(run_e1b_local.subprocess,'check_output',return_value='sha256:fixture'), patch.object(run_e1b_local,'run_open_loop',new=AsyncMock(return_value={'terminal_counts':{'failed':1}})):
                with self.assertRaisesRegex(RuntimeError,'Local replay failed'):
                    asyncio.run(run_e1b_local.execute(root,'fixed','low','mcp','fixture',root/'run'))
            self.assertTrue((root/'run/request_metrics.jsonl').exists())

    def test_analysis_matched_control_and_missing_results(self):
        # Synthetic metrics test arithmetic only; never represent measured costs/performance.
        with tempfile.TemporaryDirectory(dir=p.ROOT/'results') as temp:
            root=Path(temp);workload=root/'workload';p.prepare(self.config,workload)
            manifest,rows=p.load(workload);runs=[]
            for scenario,latency in [('fixed',100),('cpu_ramp',300)]:
                run=root/scenario;run.mkdir();runs.append(run)
                selected=[r for r in rows if r['scenario']==scenario and r['window_role']=='low' and float(r['relative_arrival_ms'])<1]
                p.write_csv(run/'planned_requests.csv',selected)
                (run/'run.json').write_text(json.dumps(dict(provider='local_mcp',window_role='low',scenario=scenario,duration_ms=1,validation_only=True,workload_manifest_sha256=p.sha(workload/'manifest.json'))))
                (run/'request_metrics.jsonl').write_text(''.join(json.dumps({**r,'status':'succeeded','end_to_end_ms':latency,'worker_cpu_seconds':None,'worker_peak_rss_bytes':None,'state_synchronization_ms':None})+'\n' for r in selected))
            report=analyze(workload,runs)
            self.assertEqual(report['reports'][1]['p95_delta_vs_matched_control_ms'],200)
            self.assertIsNone(report['reports'][1]['total_cost_cny'])
            self.assertEqual(report['reports'][1]['cost_status'],'not_applicable_local_validation')
            self.assertEqual(report['reports'][1]['worker_cpu_seconds']['count'],0)
            self.assertTrue(all(not r['complete'] and not r['winners'] for r in report['oracle']))
            (runs[0]/'request_metrics.jsonl').write_text('')
            with self.assertRaises(ValueError):analyze(workload,runs)


if __name__=='__main__':unittest.main()
