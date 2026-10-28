"""Diagnostic only: original captured programs, executed in the container Tool Runtime."""
import json
import sys
from pathlib import Path
import implementation as tool


def main():
    events=[json.loads(line) for line in Path('/work/capture/events.jsonl').read_text().splitlines()]
    out=Path('/work/diagnostic');out.mkdir(exist_ok=False)
    observed={}
    try:
        for event in events:
            if event['kind']!='tool':continue
            args={**event['args'],'uid':'architecture-calibration'}
            if event['tool_name']=='merge_artifacts':
                args['branches']=[{**ref,'artifacts':[{k:a[k] for k in ('name','signature','comparison')} for a in observed[ref['branch_ref']]['artifacts']]} for ref in args['branches']]
            result=tool.registry.functions[event['tool_name']].execute_from_dict(args)
            observed[event['key']]=result
            (out/(event['key']+'.json')).write_text(json.dumps(result,indent=2))
            print(event['key'],result.get('status'),flush=True)
    finally:
        if tool._mcp_kernel:tool._mcp_kernel.close()


if __name__=='__main__':main()
