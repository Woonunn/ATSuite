import ast
from pathlib import PurePosixPath
import re


from benchmarks.DataSciBench.nodes.AgentTools.guards import identifier,relative_file


def validate_plan(decision,prompt):
    steps=decision.get('steps')
    if not isinstance(steps,list) or not 1<=len(steps)<=10:
        raise ValueError('Plan must contain 1–10 genuine tasks')
    ids=set();outputs=set();code_hashes=set()
    for step in steps:
        name=identifier(step['id'])
        if name in ids:raise ValueError('Duplicate task ID')
        ids.add(name)
        if not isinstance(step.get('deps'),list) or not all(isinstance(x,str) for x in step['deps']):
            raise ValueError('Dependencies must be task IDs')
        normalize=lambda text:re.sub(r'\s+',' ',re.sub(r'["`]+','"',text)).casefold()
        if not step.get('purpose') or not step.get('prompt_evidence') or normalize(step['prompt_evidence']) not in normalize(prompt):
            raise ValueError('Each task needs evidence from the original prompt')
        if not isinstance(step.get('code'),str) or len(step['code'])>60000:
            raise ValueError('Missing or excessive Python code')
        normalized=ast.dump(ast.parse(step['code']),include_attributes=False)
        if normalized in code_hashes:raise ValueError('Duplicating a computation does not establish parallelism')
        code_hashes.add(normalized)
        if not isinstance(step.get('outputs'),list):raise ValueError('Outputs must be a list')
        for output in step['outputs']:
            relative_file(output)
            if output in {'state.pkl','stdout.txt','branch_manifest.json'}:raise ValueError('Reserved runtime output name')
            if output in outputs:raise ValueError('Conflicting output paths in plan')
            outputs.add(output)
    done=set()
    while len(done)<len(ids):
        ready={s['id'] for s in steps if s['id'] not in done and set(s['deps'])<=done}
        if not ready:raise ValueError('Cyclic, self-referencing or unknown dependencies')
        done.update(ready)
    return steps
