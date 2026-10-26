"""Synthetic regression fixtures; actual Copilot validation is a separate recorded run."""
import asyncio
import csv
import gzip
import copy
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from experiments.exp1.data.extract_copilot_arrivals import extract,load_config,timestamp_ns,window_id
from experiments.exp1.data.prepare_copilot import extract as unpack
from experiments.exp1.runner.arrival_scheduler import Arrival,run_open_loop
from experiments.exp1.analysis.traffic import analyze
from experiments.exp1.runner.validate_arrivals import load_span


def call(timestamp,kind='user',duration=1):return {'timestamp':timestamp,'initiator_type':kind,'duration_ms':duration}
def record(sid,calls,tid='turn',day='2026-06-01'):return {'session_id':sid,'date':day,'turns':[{'turn_id':tid,'llm_calls':calls}]}


def partition(root,day,rows):
    p=root/f'date={day}';p.mkdir(parents=True);shard=p/'shard-0000.jsonl.gz'
    shard.write_bytes(gzip.compress(('\n'.join(json.dumps(r) for r in rows)+'\n').encode(),mtime=0))
    m={'complete':True,'date':day,'n_sessions':len(rows),'n_shards':1,'shards':[{'shard_idx':0,'n_sessions':len(rows),'size_bytes':shard.stat().st_size}]}
    (p/'manifest.json').write_text(json.dumps(m))
    files=[{'name':f.name,'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in p.iterdir()]
    (p/'_verified.json').write_text(json.dumps({'archive_sha256':'synthetic fixture','files':files}))


def fixture_lock(root,config):
    p=root/'fixture-source.lock.json';p.write_text(json.dumps({'assets':[{'name':f'date.{day}.tar.gz','digest':'sha256:synthetic fixture'} for day in config['expected_dates']]}));return p


class ExtractionTests(unittest.TestCase):
    def test_global_first_user_not_each_turn_no_promotion_and_nanoseconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);raw=root/'raw';out=root/'out';cfg=load_config();cfg['expected_dates']=['2026-06-01','2026-06-02'];cfg['window_minutes']=60
            ts=lambda s:'2026-06-01T00:00:'+s+'Z'
            rows=[record('a',[call(ts('01'),'agent'),call(ts('10')),call(ts('20'))]),
                record('b',[call(ts('05'),duration=None),call(ts('30'))]),record('c',[call(ts('01'),'agent')]),
                record('d',[call(ts('01'),None),call(ts('02'))]),record('e',[call('invalid'),call(ts('06'))]),
                record('f',[call('2020-01-01T00:00:00Z'),call(ts('06'))]),record('g',[call(ts('07'))],tid=None),
                record('h',[call(ts('03')),call(ts('04'),None)]),
                record('i',[call('2026-05-31T23:59:59Z'),call(ts('05'))])]
            partition(raw,'2026-06-01',rows)
            partition(raw,'2026-06-02',[record('a',[call(ts('09.123456789'))],tid='earlier',day='2026-06-02')])
            report=extract(raw,out,cfg,fixture_lock(root,cfg))
            values=list(csv.DictReader((out/'arrivals.csv').read_text().splitlines()));source=list(csv.DictReader((out/'arrival_sources.csv').read_text().splitlines()))
            self.assertEqual(len(values),3);self.assertEqual([r['session_id'] for r in source],['h','b','a'])
            self.assertEqual(values[-1]['relative_arrival_ms'],'9123.456789')
            self.assertEqual(source[-1]['turn_id'],'earlier');self.assertEqual(source[1]['duration_ms'],'')
            self.assertEqual(report['counts']['duplicate_session_records'],1)
            self.assertEqual(report['excluded_sessions']['no_explicit_user_call'],1)
            self.assertEqual(report['excluded_sessions']['first_user_outside_observation_week'],1)
            self.assertEqual(report['excluded_sessions']['first_user_cross_date_anomaly'],1)
            analysis=analyze(out/'arrivals.csv',root/'analysis',cfg)
            windows=list(csv.DictReader((root/'analysis/windows.csv').read_text().splitlines()))
            self.assertEqual(len(windows),48)
            ordered=sorted(windows,key=lambda w:(int(w['arrival_count']),w['start_utc']))
            for name,q in cfg['selection']['quantiles'].items():self.assertEqual(analysis['selected_windows'][name]['window_id'],ordered[int(q*47)]['window_id'])
            self.assertEqual(analysis['arrivals'],3)
            before=(out/'arrivals.csv').read_bytes()
            for length,candidates in ((10,288),(15,192)):
                shorter=copy.deepcopy(cfg);shorter['window_minutes']=length
                destination=root/f'analysis-{length}'
                summary=analyze(out/'arrivals.csv',destination,shorter)
                self.assertEqual(summary['candidate_window_count'],candidates)
                self.assertEqual(summary['source_arrival_window_minutes'],60)
                self.assertNotEqual(summary['selection_policy_sha256'],report['policy_sha256'])
                self.assertTrue(all(w['duration_ms']==length*60000 for w in summary['selected_windows'].values()))
                self.assertEqual((out/'arrivals.csv').read_bytes(),before)
            changed=copy.deepcopy(cfg);changed['partition_grace_hours']=48
            with self.assertRaisesRegex(ValueError,'Only window length'):analyze(out/'arrivals.csv',root/'wrong-policy',changed)

    def test_short_window_reads_legacy_hour_annotations_without_changing_intervals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=root/'arrivals.csv';traffic=root/'traffic';traffic.mkdir()
            rows=[]
            origin=timestamp_ns('2026-06-01T00:00:00Z')
            for i,stamp in enumerate(('2026-06-01T00:15:00Z','2026-06-01T00:19:59.123456789Z','2026-06-01T00:20:00Z')):
                ns=timestamp_ns(stamp);delta=ns-origin
                rows.append({'arrival_id':str(i),'original_timestamp_utc':stamp,'relative_arrival_ms':f'{delta//1000000}.{delta%1000000:06d}',
                    'source_partition':'date=2026-06-01','source_type':'synthetic_test_fixture','window_id':window_id(ns)})
            with data.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            selected={'window_id':window_id(timestamp_ns('2026-06-01T00:10:00Z'),10),'start_utc':'2026-06-01T00:10:00Z','duration_ms':600000}
            (traffic/'traffic_summary.json').write_text(json.dumps({'selected_windows':{'high':selected},'source_arrival_window_minutes':60,
                'arrivals_path':str(data),'arrivals_sha256':hashlib.sha256(data.read_bytes()).hexdigest(),
                'origin_timestamp_utc':'2026-06-01T00:00:00Z','selection_policy_sha256':'synthetic-fixture'}))
            result,_=load_span(traffic,'high',0,600000)
            self.assertEqual([r.arrival_id for r in result],['0','1'])
            self.assertEqual([r.relative_arrival_ms for r in result],['300000.000000','599123.456789'])
            self.assertTrue(all(r.window_id==selected['window_id'] for r in result))
            self.assertEqual(window_id(timestamp_ns('2026-06-01T00:19:59Z'),15),'copilot-20260601T001500Z-PT15M')
            for row in rows:row['window_id']=window_id(timestamp_ns(row['original_timestamp_utc']),10)
            with data.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            definition=json.loads((traffic/'traffic_summary.json').read_text())
            definition.update(source_arrival_window_minutes=10,arrivals_sha256=hashlib.sha256(data.read_bytes()).hexdigest())
            definition['selected_windows']['high']={'window_id':'copilot-20260601T001500Z-PT15M','start_utc':'2026-06-01T00:15:00Z','duration_ms':900000}
            (traffic/'traffic_summary.json').write_text(json.dumps(definition))
            result,_=load_span(traffic,'high',0,900000)
            self.assertEqual([r.arrival_id for r in result],['0','1','2'])
            self.assertEqual([r.relative_arrival_ms for r in result],['0.000000','299123.456789','300000.000000'])


    def test_changed_shard_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=load_config();cfg['expected_dates']=['2026-06-01']
            partition(root/'raw','2026-06-01',[record('a',[call('2026-06-01T00:00:00Z')])])
            (root/'raw/date=2026-06-01/shard-0000.jsonl.gz').write_bytes(b'changed')
            with self.assertRaises(ValueError):extract(root/'raw',root/'out',cfg,fixture_lock(root,cfg))
            self.assertFalse((root/'out/arrivals.csv').exists());self.assertEqual(json.loads((root/'out/cleaning_report.json').read_text())['status'],'failed')

    def test_partition_is_bound_to_the_pinned_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=load_config();cfg['expected_dates']=['2026-06-01']
            partition(root/'raw','2026-06-01',[record('a',[call('2026-06-01T00:00:00Z')])])
            lock=fixture_lock(root,cfg);lock.write_text(json.dumps({'assets':[{'name':'date.2026-06-01.tar.gz','digest':'sha256:different-release'}]}))
            with self.assertRaisesRegex(ValueError,'pinned release asset'):extract(root/'raw',root/'out',cfg,lock)

    def test_requires_timezone_and_preserves_submicrosecond_precision(self):
        self.assertEqual(timestamp_ns('2026-06-01T00:00:00.000000001Z')-timestamp_ns('2026-06-01T00:00:00Z'),1)
        self.assertEqual(timestamp_ns('2026-06-01T01:00:00+01:00'),timestamp_ns('2026-06-01T00:00:00Z'))
        for value in ('2026-06-01T00:00:00','2026-02-30T00:00:00Z',None):
            with self.assertRaises((ValueError,TypeError)):timestamp_ns(value)

    def test_tar_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=root/'unsafe.tar.gz';target=root/'raw';target.mkdir()
            with tarfile.open(archive,'w:gz') as f:f.addfile(tarfile.TarInfo('../escape'))
            with self.assertRaises(ValueError):unpack(archive,target,'2026-06-01')
            self.assertFalse((root/'escape').exists())


def arrivals(offsets):return [Arrival(str(i),str(offset),'2026-06-01T00:00:00Z','date=2026-06-01','synthetic_test_fixture','test') for i,offset in enumerate(offsets)]


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_loop_submits_later_requests_before_first_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'run';gate=asyncio.Event();first=asyncio.Event()
            async def handler(a,started):started();first.set();await gate.wait();return {'ok':True}
            task=asyncio.create_task(run_open_loop(arrivals([0,10,20,20]),handler,out,duration_ms=60,concurrency=1,queue_capacity=8))
            await asyncio.wait_for(first.wait(),2);await asyncio.sleep(.05)
            events=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
            self.assertEqual(sum(e['kind']=='request_submitted' for e in events),4)
            self.assertFalse(any(e['kind']=='request_terminal' for e in events))
            gate.set();result=await task
            self.assertEqual(result['terminal_counts'],{'succeeded':4});self.assertEqual(result['submission_loss_rate'],0)
            self.assertGreaterEqual(result['peak_queue_depth'],3);self.assertGreater(result['queue_wait_to_harness_ms']['max'],20)

    async def test_overload_failure_and_admission_rejection_are_accounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=asyncio.Event();first=asyncio.Event();out=Path(tmp)/'run'
            async def handler(a,started):
                started();first.set();await gate.wait()
                if a.arrival_id=='1':raise ValueError('deliberate fixture failure')
            task=asyncio.create_task(run_open_loop(arrivals([0,5,10]),handler,out,duration_ms=30,concurrency=1,queue_capacity=1))
            await asyncio.wait_for(first.wait(),2);await asyncio.sleep(.04);gate.set();result=await task
            self.assertEqual(result['terminal_counts'],{'succeeded':1,'failed':1,'rejected':1})
            self.assertEqual(result['planned'],3);self.assertEqual(result['submitted'],3);self.assertEqual(result['unaccounted_requests'],0)
            self.assertEqual(len(list(csv.DictReader((out/'actual_arrivals.csv').read_text().splitlines()))),3)

    async def test_time_scaling_requires_explicit_label_and_missing_start_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            async def handler(a,started):return {'does_not_report_start':True}
            with self.assertRaises(ValueError):await run_open_loop(arrivals([0]),handler,Path(tmp)/'bad',duration_ms=20,time_scale=2)
            result=await run_open_loop(arrivals([0]),handler,Path(tmp)/'scaled',duration_ms=20,time_scale=2,trace_scaled=True)
            self.assertEqual(result['scenario_type'],'Trace-Scaled Scenario');self.assertEqual(result['terminal_counts'],{'failed':1})
            self.assertIsNone(result['harness_start_lateness_ms']['mean'])

    async def test_abort_records_every_planned_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            first=asyncio.Event()
            async def handler(a,started):started();first.set();await asyncio.Event().wait()
            out=Path(tmp)/'run';task=asyncio.create_task(run_open_loop(arrivals([0,500]),handler,out,duration_ms=600))
            await asyncio.wait_for(first.wait(),2);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            result=json.loads((out/'summary.json').read_text());self.assertEqual(result['terminal_counts'],{'cancelled':2});self.assertEqual(result['submitted'],1)
            self.assertEqual(result['submission_loss_rate'],.5);self.assertEqual(result['unaccounted_requests'],0)
