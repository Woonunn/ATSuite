"""Run paired E1-A groups in the precommitted, seeded provider order."""
import argparse
import asyncio
import json
from pathlib import Path
import signal
from dotenv import load_dotenv
from experiments.exp1.data.prepare_e1a import ROOT,sha
from experiments.exp1.runner.static_deployment_runner import execute
from experiments.exp1.runner.result_collector import collect_local


async def run(workload,deployment,output,*,pilot=False,admission=None):
    manifest=json.loads((Path(workload)/'manifest.json').read_text())
    if len(manifest['config']['sequence_seeds'])!=1:raise ValueError('This E1-A uses exactly one sequence seed')
    if sha(Path(workload)/'requests.csv')!=manifest['request_table_sha256']:raise ValueError('Request table changed')
    output=Path(output);output.mkdir(parents=True,exist_ok=False);reports=[]
    matrix={'status':'running','groups':[
        {'sequence_seed':group['sequence_seed'],'window_role':group['window_role'],'provider':provider,'status':'not_started'}
        for group in (manifest['groups'][:1] if pilot else manifest['groups']) for provider in group['provider_order']]}
    state_path=output/'matrix.json'
    state_path.write_text(json.dumps(matrix,indent=2)+'\n')
    try:
        for group in matrix['groups']:
            directory=output/f"{group['sequence_seed']}-{group['window_role']}-{group['provider']}"
            group['status']='running';state_path.write_text(json.dumps(matrix,indent=2)+'\n')
            summary=await execute(workload,deployment,group['provider'],group['sequence_seed'],group['window_role'],directory,pilot=pilot,admission=admission)
            if summary['status']!='complete':raise RuntimeError('Window did not complete; stopping the entire matrix: '+str(directory))
            report=collect_local(directory);reports.append(report)
            (output/'reports.json').write_text(json.dumps(reports,indent=2)+'\n')
            group['status']='complete'
            if pilot and report['successes']!=report['requests']:raise RuntimeError('Pilot failed; diagnose before submitting more paid requests: '+str(directory))
    except BaseException as exc:
        matrix['status']='interrupted' if isinstance(exc,(asyncio.CancelledError,KeyboardInterrupt)) else 'failed'
        matrix['error']={'type':type(exc).__name__,'message':str(exc)}
        group['status']=matrix['status']
        if (directory/'schedule/summary.json').exists():
            try:
                report=collect_local(directory)
                if report not in reports:reports.append(report)
                (output/'reports.json').write_text(json.dumps(reports,indent=2)+'\n')
            except Exception as error:matrix['collection_error']={'type':type(error).__name__,'message':str(error)}
        raise
    else:matrix['status']='complete'
    finally:state_path.write_text(json.dumps(matrix,indent=2)+'\n')
    return reports


async def main(args):
    """Let SIGTERM cancel the matrix while the scheduler records all terminals."""
    loop=asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM,asyncio.current_task().cancel)
    try:return await run(args.workload,args.deployment,args.output,pilot=args.pilot,admission=args.admission)
    finally:loop.remove_signal_handler(signal.SIGTERM)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workload',required=True,type=Path);p.add_argument('--deployment',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path);p.add_argument('--pilot',action='store_true');p.add_argument('--admission',type=Path)
    a=p.parse_args();load_dotenv(ROOT/'.env');print(json.dumps(asyncio.run(main(a)),indent=2))
