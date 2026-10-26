"""Verify all official-metric mapping rows against the pinned local checkout, offline."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import yaml
ROOT=Path(__file__).resolve().parents[3]


def validate(upstream):
    mapping=json.loads((ROOT/'benchmarks/DataSciBench/tasks/evaluation_alignment.json').read_text())
    manifest=json.loads((ROOT/'benchmarks/DataSciBench/tasks/manifest.json').read_text())
    tasks={t['task_id']:t for t in manifest['tasks']}
    assert {t['task_id'] for t in mapping['tasks']}==set(tasks),'Selected task coverage changed'
    count=0
    for task in mapping['tasks']:
        assert task['prompt_sha256']==tasks[task['task_id']]['prompt_sha256'],'Prompt changed; review evaluation mapping'
        raw=subprocess.check_output(['git','-C',str(upstream),'show',mapping['source_commit_sha']+':'+task['official_metric_path']])
        assert hashlib.sha256(raw).hexdigest()==task['official_metric_sha256'],'Official metric source changed'
        entries=yaml.safe_load(raw)['TMC-list']
        assert len(entries)==len(task['metrics']),'Unmapped official metric'
        for i,(entry,row) in enumerate(zip(entries,task['metrics'])):
            assert row['index']==i and row['metric']==entry['metric'] and row['function']==entry['function']
            assert row['ground_truth']==entry.get('ground_truth')
            assert row['code_sha256']==hashlib.sha256(entry['code'].encode()).hexdigest()
            assert row['coverage'] in ('covered','partial','not_covered')
            assert row['custom_check'] and row['difference_or_gap']
            count+=1
        assert task['official_pass'] is None,'Custom checks must not manufacture an official result'
    return {'tasks':len(tasks),'official_metrics_mapped':count,'official_scores':'not claimed','status':'passed'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--upstream',type=Path,default=ROOT/'results/upstream/DataSciBench')
    print(json.dumps(validate(p.parse_args().upstream)))
