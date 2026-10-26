"""Re-execute recorded real DeepSeek decisions through LangGraph on both lifecycles."""
import argparse
import asyncio
import json
from pathlib import Path
import uuid
from experiments.exp1.capture.capture_agent_traces import run
from experiments.exp1.tests.check_agent_lifecycle import ROOT,service


def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);a=p.parse_args()
    area=ROOT/'results/phase2'/('resident-replays-'+uuid.uuid4().hex);area.mkdir(parents=True)
    manifest=json.loads((ROOT/'benchmarks/DataSciBench/trace/captured/manifest.json').read_text())
    base=json.loads((ROOT/'benchmarks/DataSciBench/config/langgraph_experiment.json').read_text());reports=[]
    for case in manifest['captures']:
        for family in ('faas','mcp'):
            target=area/(case['task_id']+'-'+family)
            with service(family,target/'environment',a.image) as config:
                result=asyncio.run(run(case['task_id'],{**base,'tool':config},target/'replay',ROOT/case['capture']))
                events=[json.loads(line) for line in (target/'replay/events.jsonl').read_text().splitlines()]
                tools=[e for e in events if e['kind']=='tool'];analyses=[e for e in tools if e['tool_name']=='execute_analysis']
                names={e['result']['container_name'] for e in analyses}
                if family=='mcp':
                    assert len(names)==1,'MCP started multiple calculation containers'
                    assert all(not e['result']['state_lifecycle']['canonical_restore_performed'] for e in analyses)
                    assert not list((target/'environment/state').glob('.snapshots/**/*.json'))
                else:
                    assert len(names)==len(analyses),'FaaS reused calculation containers'
                    assert all(e['result']['state_lifecycle']['canonical_restore_performed'] for e in analyses)
                overlap=[]
                for i,x in enumerate(analyses):
                    for y in analyses[i+1:]:
                        if x['wave']!=y['wave']:continue
                        u,v=x['result'],y['result']
                        duration=min(u['exec_ended_monotonic'],v['exec_ended_monotonic'])-max(u['exec_started_monotonic'],v['exec_started_monotonic'])
                        if duration>0:overlap.append({'steps':[x['key'],y['key']],'seconds':duration})
                reports.append({**result,'family':family,'analysis_containers':len(names),'tool_calls':len(tools),
                    'parallel_execution':overlap,'validation':json.loads((target/'replay/result.json').read_text())['validation']})
                print(case['task_id'],family,'passed',flush=True)
    (area/'validation.json').write_text(json.dumps({'scope':'new real Tool execution, recorded DeepSeek decisions and delays; no new LLM charges; no cloud billing',
        'image':a.image,'cases':reports},indent=2)+'\n');print(area)


if __name__=='__main__':main()
