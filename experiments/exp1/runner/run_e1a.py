"""Run paired E1-A groups in the precommitted, seeded provider order."""
import argparse
import asyncio
import json
from pathlib import Path
from dotenv import load_dotenv
from experiments.exp1.data.prepare_e1a import ROOT,sha
from experiments.exp1.runner.static_deployment_runner import execute
from experiments.exp1.runner.result_collector import collect_local


async def run(workload,deployment,output,*,pilot=False,admission=None):
    manifest=json.loads((Path(workload)/'manifest.json').read_text())
    if len(manifest['config']['sequence_seeds'])!=1:raise ValueError('This E1-A uses exactly one sequence seed')
    if sha(Path(workload)/'requests.csv')!=manifest['request_table_sha256']:raise ValueError('Request table changed')
    output=Path(output);output.mkdir(parents=True,exist_ok=False);reports=[]
    for group in manifest['groups'][:1] if pilot else manifest['groups']:
        for provider in group['provider_order']:
            directory=output/f"{group['sequence_seed']}-{group['window_role']}-{provider}"
            await execute(workload,deployment,provider,group['sequence_seed'],group['window_role'],directory,pilot=pilot,admission=admission)
            report=collect_local(directory);reports.append(report)
            (output/'reports.json').write_text(json.dumps(reports,indent=2)+'\n')
            if pilot and report['successes']!=report['requests']:raise RuntimeError('Pilot failed; diagnose before submitting more paid requests: '+str(directory))
    return reports


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workload',required=True,type=Path);p.add_argument('--deployment',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path);p.add_argument('--pilot',action='store_true');p.add_argument('--admission',type=Path)
    a=p.parse_args();load_dotenv(ROOT/'.env');print(json.dumps(asyncio.run(run(a.workload,a.deployment,a.output,pilot=a.pilot,admission=a.admission)),indent=2))
