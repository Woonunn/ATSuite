"""The legacy and structured Tools share this execution and output-capture primitive."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DataSciKernelTests(unittest.TestCase):
    def test_native_output_errors_and_namespace_continuity(self):
        with tempfile.TemporaryDirectory() as directory:
            code='''
import json,sys
from pathlib import Path
from benchmarks.DataSciBench.nodes.DataSci.kernel import execute_code
scope={}
log=Path(sys.argv[1])/'stdout.txt'
a=execute_code("import os\\nvalue=7\\nprint('python-output')\\nos.write(1,b'native-output\\\\n')",scope,log)
b=execute_code("value*=3\\nraise ValueError('failure-keeps-state')",scope,log)
c=execute_code("assert value==21\\nprint(value)",scope,log)
print(json.dumps({'a':a,'b':b,'c':c}))
'''
            result=json.loads(subprocess.check_output([sys.executable,'-c',code,directory],text=True))
            self.assertEqual(result['a']['status'],'ok')
            self.assertIn('python-output',result['a']['output'])
            self.assertIn('native-output',result['a']['output'])
            self.assertEqual(result['b']['status'],'error')
            self.assertIn('ValueError: failure-keeps-state',result['b']['error'])
            self.assertEqual(result['c']['output'],'21\n')

    def test_evaluation_mapping_covers_every_selected_task_without_fabricated_scores(self):
        root=Path(__file__).resolve().parents[1]/'benchmarks/DataSciBench/tasks'
        mapping=json.loads((root/'evaluation_alignment.json').read_text())
        manifest=json.loads((root/'manifest.json').read_text())
        tasks={t['task_id']:t for t in manifest['tasks']}
        self.assertEqual({t['task_id'] for t in mapping['tasks']},set(tasks))
        for task in mapping['tasks']:
            self.assertEqual(task['prompt_sha256'],tasks[task['task_id']]['prompt_sha256'])
            self.assertIsNone(task['official_pass'])
            for metric in task['metrics']:
                self.assertIn(metric['coverage'],('covered','partial','not_covered'))
                self.assertTrue(metric['custom_check'] and metric['difference_or_gap'])
