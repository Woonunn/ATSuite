import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.exp1.data.prepare_datasets import checked, prepare
from experiments.exp1.profile.profile_tasks import publish, run_profiles


class DatasetIntegrityTests(unittest.TestCase):
    def test_checksum_rejects_tampering_and_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'input.csv';p.write_bytes(b'original\n')
            record={'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            checked(p,record)
            p.write_bytes(b'changed\n')
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):checked(p,record)
            p.unlink()
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):checked(p,record)

    def test_locked_source_change_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);lock=root/'lock.json'
            lock.write_text(json.dumps({'tasks':{'human_12':{'status':'prepared','source_sha256':'expected',
                  'files':[{'name':'adult.data','sha256':'expected'}]}}}))
            def changed_download(url,path,token=None):path.write_bytes(b'changed upstream source')
            with patch('experiments.exp1.data.prepare_datasets.SOURCES',{'human_12':('https://example.invalid/source.zip',['adult.data'],'test')}),patch('experiments.exp1.data.prepare_datasets.download',changed_download):
                with self.assertRaisesRegex(ValueError,'Upstream bytes changed'):prepare(root/'inputs',lock)
            self.assertEqual(json.loads(lock.read_text())['tasks']['human_12']['source_sha256'],'expected')
            self.assertFalse((root/'inputs/human_12/adult.data').exists())

    def test_failed_profile_cannot_publish_over_existing_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);tasks=root/'benchmarks/DataSciBench/tasks';tasks.mkdir(parents=True)
            (tasks/'catalog.json').write_text(json.dumps({'tasks':[],'task_count':222}))
            (tasks/'datasets.lock.json').write_text(json.dumps({'tasks':{}}))
            output=tasks/'manifest.json';output.write_text('previous valid manifest')
            (root/'batch.json').write_text(json.dumps({'environment':{'repetitions':3},'runs':[
                {'task_id':'human_12','status':'timeout','profile':None}]}))
            with patch('experiments.exp1.profile.profile_tasks.ROOT',root):
                with self.assertRaisesRegex(ValueError,'unsuccessful'):publish(root)
            self.assertEqual(output.read_text(),'previous valid manifest')

    def test_missing_dataset_fails_before_docker_execution(self):
        with tempfile.TemporaryDirectory() as d:
            lock=Path(d)/'lock.json';lock.write_text('{"tasks": {}}')
            with patch('experiments.exp1.data.prepare_datasets.LOCK',lock),patch('experiments.exp1.profile.profile_tasks.subprocess.run') as run:
                with self.assertRaisesRegex(ValueError,'Unprepared task'):run_profiles('unused',['human_12'])
                run.assert_not_called()


class ProfileAggregationTests(unittest.TestCase):
    def test_different_environments_or_duplicate_tasks_cannot_be_combined(self):
        from experiments.exp1.analysis.aggregate import aggregate
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);a=root/'a';b=root/'b';a.mkdir();b.mkdir()
            for path,seed in [(a,0),(b,1)]:
                (path/'batch.json').write_text(json.dumps({'environment':{'seed':seed},'runs':[{'task_id':'human_12'}]}))
            with self.assertRaisesRegex(ValueError,'different environments'):aggregate([a,b])
            (b/'batch.json').write_bytes((a/'batch.json').read_bytes())
            with self.assertRaisesRegex(ValueError,'Overlapping tasks'):aggregate([a,b])


class PreparationReplayTests(unittest.TestCase):
    def test_access_denied_is_recorded_and_inline_input_is_still_prepared(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);tasks=root/'benchmarks/DataSciBench/tasks';tasks.mkdir(parents=True)
            text='Month,Year,Sales,Category\nJanuary,2022,1500,Electronics'
            catalog={'source':{'repository':'https://github.com/THUDM/DataSciBench','commit_sha':'fixture'},
                     'tasks':[{'task_id':'human_15','prompt':'```\n'+text+'\n```','prompt_sha256':'fixture'}, {'task_id':'csv_excel_29'}]}
            (tasks/'catalog.json').write_text(json.dumps(catalog))
            lock_path=tasks/'datasets.lock.json';data=root/'inputs'
            sources={'csv_excel_29':('https://example.invalid/data.csv',['data.csv'],'gated test')}
            denied=urllib.error.HTTPError(sources['csv_excel_29'][0],401,'Unauthorized',None,None)
            with patch('experiments.exp1.data.prepare_datasets.ROOT',root),patch('experiments.exp1.data.prepare_datasets.SOURCES',sources),patch('experiments.exp1.data.prepare_datasets.download',side_effect=denied):
                lock=prepare(data,lock_path)
                self.assertEqual(lock['tasks']['csv_excel_29']['http_status'],401)
                self.assertEqual((data/'human_15/sales.csv').read_text(),text+'\n')
                self.assertEqual(prepare(data,lock_path),lock)
                (data/'human_15/sales.csv').write_text('corrupted')
                with self.assertRaisesRegex(ValueError,'checksum mismatch'):prepare(data,lock_path)

    def test_excluded_tasks_are_not_downloaded_or_retained_in_lock(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);tasks=root/'benchmarks/DataSciBench/tasks';tasks.mkdir(parents=True)
            catalog={'source':{'repository':'fixture','commit_sha':'fixture'},
                     'tasks':[{'task_id':'human_15','prompt':'```\nMonth,Year,Sales,Category\nJanuary,2022,1500,Electronics\n```','prompt_sha256':'fixture'}]}
            (tasks/'catalog.json').write_text(json.dumps(catalog))
            lock_path=tasks/'datasets.lock.json'
            lock_path.write_text(json.dumps({'tasks':{'csv_excel_29':{'status':'blocked'}}}))
            with patch('experiments.exp1.data.prepare_datasets.ROOT',root),patch('experiments.exp1.data.prepare_datasets.download') as download:
                lock=prepare(root/'inputs',lock_path)
            download.assert_not_called()
            self.assertEqual(set(lock['tasks']),{'human_15'})
            self.assertFalse((root/'inputs/csv_excel_29').exists())


if __name__=='__main__':unittest.main()
