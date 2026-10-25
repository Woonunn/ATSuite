"""Container entry: route reference computation through the existing DataSci Tool."""
import json
import os
from pathlib import Path
import sys
import time
import traceback
from datetime import datetime, timezone
from benchmarks.DataSciBench.nodes.DataSci.implementation import datasci_run_code


def main():
    task,run_id,seed=sys.argv[1],sys.argv[2],int(sys.argv[3])
    os.environ['ATSUITE_PROFILE_RUN_ID']=run_id
    start=time.perf_counter();record={'run_id':run_id,'request_id':run_id+'-tool','task_id':task,'utc':datetime.now(timezone.utc).isoformat(),'status':'failed'}
    try:
        code=f'from experiments.exp1.profile.reference_tasks import run\nrun({task!r}, {("/inputs/"+task)!r}, "/out/artifacts", {seed})'
        result=datasci_run_code.execute_from_dict({'uid':run_id,'code_str':code})
        print(result);record.update(status='ok',tool_output=result)
    except BaseException as e:
        record.update(error_type=type(e).__name__,error=str(e),traceback=traceback.format_exc(),tool_output=getattr(e,'result',None))
        raise
    finally:
        record['monotonic_seconds']=time.perf_counter()-start
        Path('/out/tool_execution.json').write_text(json.dumps(record,indent=2)+'\n')


if __name__=='__main__':main()
