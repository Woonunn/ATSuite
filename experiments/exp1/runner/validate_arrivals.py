"""Replay an explicit original-time span into a lightweight local CPU Harness."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
from pathlib import Path
from experiments.exp1.runner.arrival_scheduler import Arrival,ms_ns,run_open_loop
from experiments.exp1.data.extract_copilot_arrivals import load_config,CONFIG,timestamp_ns,milliseconds,window_id
from experiments.exp1.data.prepare_copilot import sha


def load_span(traffic,role,offset_ms,duration_ms):
    traffic=Path(traffic);summary=json.loads((traffic/'traffic_summary.json').read_text());window=summary['selected_windows'][role]
    start=ms_ns(offset_ms);duration=ms_ns(duration_ms)
    if start+duration>ms_ns(window['duration_ms']):raise ValueError('Validation slice exceeds selected window')
    path=Path(summary['arrivals_path'])
    if sha(path)!=summary['arrivals_sha256']:raise ValueError('Source arrival checksum changed')
    window_offset=timestamp_ns(window['start_utc'])-timestamp_ns(summary['origin_timestamp_utc']);begin=window_offset+start
    result=[]
    with path.open() as f:
        for row in csv.DictReader(f):
            delta=ms_ns(row['relative_arrival_ms'])
            if begin<=delta<begin+duration:
                original_ns=timestamp_ns(row['original_timestamp_utc'])
                if row['window_id']!=window_id(original_ns,summary.get('source_arrival_window_minutes',60)):raise ValueError('Incorrect source window assignment')
                result.append(Arrival(**{**row,'relative_arrival_ms':milliseconds(delta-begin),'window_id':window['window_id']}))
    return result,{'role':role,'selected_window':window,'source_arrivals_sha256':summary['arrivals_sha256'],
        'source_selection_policy_sha256':summary['selection_policy_sha256'],'slice_offset_ms':str(offset_ms),'slice_duration_ms':str(duration_ms),
        'validation_only':True,'traffic_transform':'none; original intervals and initial/trailing idle periods retained within the explicit slice'}


async def validate(traffic,role,output,config,*,duration_ms=None,offset_ms=None,concurrency=None,queue_capacity=None,time_scale=1.0,trace_scaled=False):
    probe=config['validation'];duration=probe['duration_ms'] if duration_ms is None else duration_ms;offset=probe['offset_ms'] if offset_ms is None else offset_ms
    arrivals,metadata=load_span(traffic,role,offset,duration)
    count=probe['concurrency'] if concurrency is None else concurrency;capacity=probe['queue_capacity'] if queue_capacity is None else queue_capacity
    iterations=int(probe['pbkdf2_iterations']);seed=int(probe['seed'])
    if iterations<1:raise ValueError('PBKDF2 iterations must be positive')
    if time_scale!=1.0 or trace_scaled:metadata['traffic_transform']='Trace-Scaled Scenario: execution offsets and duration divided by explicit time_scale; original source timestamps retained'
    metadata.update(harness='local PBKDF2 scheduling probe, not a DataSciBench task/model or cloud bill',iterations=iterations,seed=seed)
    with ThreadPoolExecutor(max_workers=count) as pool:
        loop=asyncio.get_running_loop()
        async def handler(arrival,started):
            def compute():
                started()
                result=hashlib.pbkdf2_hmac('sha256',arrival.arrival_id.encode(),str(seed).encode(),iterations)
                return {'digest':result.hex()}
            return await loop.run_in_executor(pool,compute)
        return await run_open_loop(arrivals,handler,output,duration_ms=duration,concurrency=count,queue_capacity=capacity,
            time_scale=time_scale,trace_scaled=trace_scaled,metadata=metadata)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--traffic',required=True,type=Path);p.add_argument('--role',required=True,choices=['low','typical','high']);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--duration-ms',type=int);p.add_argument('--offset-ms',type=int);p.add_argument('--concurrency',type=int);p.add_argument('--queue-capacity',type=int)
    p.add_argument('--time-scale',type=float,default=1.0);p.add_argument('--trace-scaled',action='store_true');p.add_argument('--config',type=Path,default=CONFIG)
    a=p.parse_args();result=asyncio.run(validate(a.traffic,a.role,a.output,load_config(a.config),duration_ms=a.duration_ms,offset_ms=a.offset_ms,
        concurrency=a.concurrency,queue_capacity=a.queue_capacity,time_scale=a.time_scale,trace_scaled=a.trace_scaled));print(json.dumps(result,indent=2))
