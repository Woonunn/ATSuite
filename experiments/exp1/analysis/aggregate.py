"""Combine disjoint profiling batches from the identical fixed reference environment."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid
from experiments.exp1.profile.profile_tasks import ROOT, publish


def aggregate(paths):
    batches=[json.loads((Path(p)/'batch.json').read_text()) for p in paths]
    if not batches:raise ValueError('At least one profiling batch is required')
    environment=batches[0]['environment']
    if any(b['environment']!=environment for b in batches):
        raise ValueError('Cannot combine different environments, code, seeds or repeat counts')
    seen=set();runs=[]
    for batch in batches:
        tasks={r['task_id'] for r in batch['runs']}
        if seen&tasks:raise ValueError('Overlapping tasks: use one complete batch per task')
        seen.update(tasks);runs.extend(batch['runs'])
    out=ROOT/'results/phase1'/('selection-'+uuid.uuid4().hex);out.mkdir(parents=True)
    result={'batch_id':out.name,'source_batches':[str(Path(p).resolve().relative_to(ROOT)) for p in paths],
            'environment':environment,'runs':runs,
            'aggregation_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (out/'batch.json').write_text(json.dumps(result,indent=2)+'\n')
    publish(out)
    print(out)
    return out


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('batches',nargs='+',type=Path)
    aggregate(parser.parse_args().batches)
