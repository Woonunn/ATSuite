"""Local Docker E1-B replay. Each MCP Session owns its service and resident kernel."""
import argparse
import asyncio
from dataclasses import fields
import json
from pathlib import Path
import subprocess
from experiments.exp1.data.prepare_e1b import ROOT, load, sha, write_csv
from experiments.exp1.capture.capture_agent_traces import run as replay
from experiments.exp1.runner.arrival_scheduler import Arrival, run_open_loop, ms_ns
from experiments.exp1.runner.result_collector import read_events, request_metrics
from experiments.exp1.tests.check_agent_lifecycle import service


async def execute(workload, scenario, role, family, image, output, *, prefix_ms=None, concurrency=2):
    manifest, all_rows = load(workload); config = manifest['config']
    duration = config['window_duration_ms'] if prefix_ms is None else prefix_ms
    if not 0 < duration <= config['window_duration_ms']: raise ValueError('Invalid local validation prefix')
    rows = [r for r in all_rows if r['scenario'] == scenario and r['window_role'] == role and ms_ns(r['relative_arrival_ms']) < ms_ns(duration)]
    if not rows: raise ValueError('No requests selected')
    if family not in ('faas', 'mcp'): raise ValueError('Only local Docker lifecycles are supported')
    image_id = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', image], text=True).strip()
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=False)
    write_csv(output/'planned_requests.csv', rows)
    info = dict(experiment='E1-B', provider='local_'+family, scenario=scenario, window_role=role,
        validation_only=True, prefix_ms=prefix_ms, duration_ms=duration, time_scale=1.0, image_id=image_id,
        workload_manifest_sha256=sha(Path(workload)/'manifest.json'), request_table_sha256=manifest['request_table_sha256'],
        scope='Real LangGraph and Docker Tool replay; recorded model decisions/delays; not cloud performance or billing')
    (output/'run.json').write_text(json.dumps(info, indent=2)+'\n')
    by_id = {r['arrival_id']:r for r in rows}
    base = json.loads((ROOT/'benchmarks/DataSciBench/config/langgraph_experiment.json').read_text())
    async def handler(arrival, started):
        started(); row = by_id[arrival.arrival_id]; directory = output/'requests'/arrival.arrival_id
        # A distinct frontend process per Session models actual MCP isolation, not shared-process isolation.
        context = service(family, output/'environments'/arrival.arrival_id, image_id)
        tool = await asyncio.to_thread(context.__enter__)
        try:
            return await replay(row['task_id'], {**base, 'tool':tool}, directory, ROOT/manifest['tasks'][row['task_id']]['capture'])
        finally:
            await asyncio.to_thread(context.__exit__, None, None, None)
    arrivals = [Arrival(**{f.name:r[f.name] for f in fields(Arrival)}) for r in rows]
    try:
        summary = await run_open_loop(arrivals, handler, output/'schedule', duration_ms=duration, concurrency=concurrency,
            queue_capacity=config['queue_capacity'], metadata=info, time_scale=1.0)
        if summary['terminal_counts'].get('succeeded', 0) != len(rows):
            raise RuntimeError('Local replay failed; inspect preserved request metrics and diagnostics')
        return summary
    finally:
        terminals = {e['arrival_id']:e for e in read_events(output/'schedule/events.jsonl') if e['kind']=='request_terminal'}
        with (output/'request_metrics.jsonl').open('w') as f:
            for row in rows:
                if row['arrival_id'] not in terminals: continue
                metrics = request_metrics(row, terminals[row['arrival_id']], output/'requests'/row['arrival_id'])
                metrics.update(provider='local_'+family, validation_only=True, cost_status='not_applicable_local_validation', cost_cny=None)
                f.write(json.dumps(metrics, allow_nan=False)+'\n')


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('workload','scenario','role','image','output'): p.add_argument('--'+name, required=True)
    p.add_argument('--family', choices=['faas','mcp'], required=True); p.add_argument('--prefix-ms', type=int); p.add_argument('--concurrency',type=int,default=2)
    a=p.parse_args(); print(json.dumps(asyncio.run(execute(a.workload,a.scenario,a.role,a.family,a.image,a.output,prefix_ms=a.prefix_ms,concurrency=a.concurrency)),indent=2))
