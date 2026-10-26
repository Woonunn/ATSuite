"""Run the fixed original-time validation slices in credential-free local Docker."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
from experiments.exp1.data.extract_copilot_arrivals import load_config,CONFIG
from experiments.exp1.data.prepare_copilot import ROOT,sha
from experiments.exp1.runner.validate_arrivals import load_span
from experiments.exp1.runner.arrival_scheduler import ms_ns


def run(traffic,output,image):
    traffic,output=Path(traffic).resolve(),Path(output).resolve()
    if not image.startswith('sha256:'):raise ValueError('Pin the local validation image')
    config=load_config();probe=config['validation'];source=json.loads((traffic/'traffic_summary.json').read_text());arrivals=Path(source['arrivals_path']).parent
    config["window_minutes"]=source.get("window_minutes",60)
    output.mkdir(parents=True,exist_ok=False)
    def one(role):
        name='wabber-arrival-'+uuid.uuid4().hex;command=['docker','run','--rm','--name',name,'--network','none','--read-only',
            '--cpus','2','--memory','2g','--memory-swap','2g','--pids-limit','128','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--user',f'{os.getuid()}:{os.getgid()}','--tmpfs','/tmp:rw,nosuid,size=256m',
            '-v',f'{ROOT / "experiments/exp1"}:/repo/experiments/exp1:ro','-v',f'{arrivals}:{arrivals}:ro',
            '-v',f'{traffic}:/traffic:ro','-v',f'{output}:/output:rw','-e','PYTHONPATH=/repo','-w','/repo','--entrypoint','python',image,
            '-m','experiments.exp1.runner.validate_arrivals','--traffic','/traffic','--role',role,'--output','/output/'+role]
        try:
            with (output/(role+'.log')).open('w') as log:subprocess.run(command,stdout=log,stderr=log,check=True)
        finally:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        summary=json.loads((output/role/'summary.json').read_text());expected,metadata=load_span(traffic,role,probe['offset_ms'],probe['duration_ms'])
        with (output/role/'actual_arrivals.csv').open() as stream:actual=list(csv.DictReader(stream))
        events=[json.loads(s) for s in (output/role/'events.jsonl').read_text().splitlines()];anchor=events[0]['anchor_monotonic_ns']
        assert [r['arrival_id'] for r in actual]==[a.arrival_id for a in expected],'Missing/duplicated/reordered planned IDs'
        for row,a in zip(actual,expected):
            assert int(row['planned_arrival_monotonic_ns'])-anchor==ms_ns(a.relative_arrival_ms),'Original relative timing changed'
            assert row['submitted_monotonic_ns'] and row['harness_started_monotonic_ns'] and row['completed_monotonic_ns']
            assert int(row['submitted_monotonic_ns'])<=int(row['harness_started_monotonic_ns'])<=int(row['completed_monotonic_ns'])
            assert row['status']=='succeeded',row
        assert summary['time_scale']==1 and summary['submitted']==summary['planned']==len(actual) and summary['unaccounted_requests']==0
        assert sum(1 for e in events if e['kind']=='harness_started')==len(actual)
        assert sum(1 for e in events if e['kind']=='request_terminal')==len(actual)
        return {'role':role,'directory':str((output/role).relative_to(ROOT)),'summary':summary,
            'independent_checks':'Exact source IDs, unchanged planned nanosecond intervals, one start/terminal per ID, actual timestamp ordering'}
    with ThreadPoolExecutor(max_workers=3) as pool:cases=list(pool.map(one,('low','typical','high')))
    assert sum(c['summary']['planned'] for c in cases)>0,'No real arrival was tested'
    sources=[CONFIG,*ROOT.joinpath('experiments/exp1/runner').glob('*.py'),Path(__file__)]
    result={'scope':'original sampled Copilot intervals in fixed 120-second validation slices; lightweight local CPU Harness, not DataSciBench/LLM work or cloud bills',
        'image':image,'resource_per_case':{'cpu':2,'memory_mib':2048,'external_network':False},'selection_policy':config,
        'traffic_summary_sha256':sha(traffic/'traffic_summary.json'),'source_sha256':{str(p.relative_to(ROOT)):sha(p) for p in sources},'cases':cases}
    (output/'validation.json').write_text(json.dumps(result,indent=2)+'\n');return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--traffic',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--image',required=True)
    a=p.parse_args();print(json.dumps(run(a.traffic,a.output,a.image),indent=2))
