"""Open-loop arrivals: admission never waits for previous request completion."""
import asyncio
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import json
import math
from pathlib import Path
import threading
import time
import traceback
from datetime import datetime,timezone
import csv


@dataclass(frozen=True)
class Arrival:
    arrival_id:str
    relative_arrival_ms:str
    original_timestamp_utc:str
    source_partition:str
    source_type:str
    window_id:str


def ms_ns(value):
    number=Decimal(str(value))
    if not number.is_finite() or number<0 or number*1000000!=int(number*1000000):raise ValueError('Invalid millisecond timestamp')
    return int(number*1000000)


def utc(ns):
    seconds,nanos=divmod(ns,10**9)
    return datetime.fromtimestamp(seconds,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')+f'.{nanos:09d}Z'


def stats(values):
    if not values:return {'count':0,'mean':None,'p50':None,'p95':None,'p99':None,'max':None}
    ordered=sorted(values)
    def q(f):
        position=(len(ordered)-1)*f;lower=math.floor(position);upper=math.ceil(position)
        return ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower)
    return {'count':len(values),'mean':sum(values)/len(values),'p50':q(.5),'p95':q(.95),'p99':q(.99),'max':ordered[-1]}


async def wait_until(deadline_ns):
    # Bounded sleeps reduce long-idle timer coalescing without busy spinning.
    while (remaining:=(deadline_ns-time.monotonic_ns())/10**9)>0:
        await asyncio.sleep(min(remaining,.1))


async def run_open_loop(arrivals,handler,output,*,duration_ms,concurrency=4,queue_capacity=1000,time_scale=1.0,trace_scaled=False,metadata=None,observer=None):
    if concurrency<1 or queue_capacity<1:raise ValueError('Capacity must be positive')
    scale=Decimal(str(time_scale))
    if not scale.is_finite() or scale<=0 or (scale!=1 and not trace_scaled):raise ValueError('Changed time_scale requires explicit trace_scaled=True')
    duration_ns=ms_ns(duration_ms);source_offsets=[ms_ns(a.relative_arrival_ms) for a in arrivals]
    if source_offsets!=sorted(source_offsets) or len({a.arrival_id for a in arrivals})!=len(arrivals):raise ValueError('Duplicate or unordered arrivals')
    if source_offsets and source_offsets[-1]>=duration_ns:raise ValueError('Arrival outside half-open observation span')
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    lock=threading.Lock();events=(output/'events.jsonl').open('w');sequence=0
    anchor_mono=time.monotonic_ns()+100000000;anchor_utc=time.time_ns()+100000000
    def emit(kind,**values):
        nonlocal sequence
        stamp=time.monotonic_ns();wall=time.time_ns()
        with lock:
            events.write(json.dumps({'seq':sequence,'kind':kind,'logged_monotonic_ns':stamp,'logged_utc':utc(wall),**values},allow_nan=False)+'\n');events.flush();sequence+=1
        if observer is not None:observer({'kind':kind,'wall_ns':wall,**values})
    records=[]
    for a,offset in zip(arrivals,source_offsets):
        planned=anchor_mono+int(Decimal(offset)/scale);wall=anchor_utc+int(Decimal(offset)/scale)
        records.append({'arrival_id':a.arrival_id,'source_type':a.source_type,'source_partition':a.source_partition,'window_id':a.window_id,
            'original_timestamp_utc':a.original_timestamp_utc,'relative_arrival_ms':a.relative_arrival_ms,
            'planned_arrival_monotonic_ns':planned,'planned_arrival_utc':utc(wall),'submitted_monotonic_ns':None,'submitted_utc':None,
            'dispatched_monotonic_ns':None,'harness_started_monotonic_ns':None,'harness_started_utc':None,
            'completed_monotonic_ns':None,'completed_utc':None,'status':'planned','error':None})
    queue=asyncio.Queue(maxsize=queue_capacity);active=0;peak_active=0;peak_queue=0
    def terminal(record,status,error=None,result=None):
        record.update(status=status,error=error,completed_monotonic_ns=time.monotonic_ns(),completed_utc=utc(time.time_ns()))
        if result is not None:record['result']=result
        emit('request_terminal',**record)
    async def worker():
        nonlocal active,peak_active
        while True:
            index=await queue.get();record=records[index];a=arrivals[index]
            active+=1;peak_active=max(peak_active,active);record['dispatched_monotonic_ns']=time.monotonic_ns()
            def started():
                if record['completed_monotonic_ns'] is not None:raise RuntimeError('Harness start after request termination')
                if record['harness_started_monotonic_ns'] is not None:raise ValueError('Harness reported start twice')
                record['harness_started_monotonic_ns']=time.monotonic_ns();record['harness_started_utc']=utc(time.time_ns());record['status']='running'
                emit('harness_started',arrival_id=a.arrival_id,started_monotonic_ns=record['harness_started_monotonic_ns'],started_utc=record['harness_started_utc'],active=active,queue_depth=queue.qsize())
            try:
                result=await handler(a,started)
                if record['harness_started_monotonic_ns'] is None:raise ValueError('Handler did not report actual Harness entry')
                if result is not None:json.dumps(result,allow_nan=False)
                terminal(record,'succeeded',result=result)
            except asyncio.CancelledError:
                terminal(record,'cancelled',{'type':'CancelledError','message':'Run interrupted'});raise
            except Exception as exc:
                terminal(record,'failed',{'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()})
            finally:active-=1;queue.task_done()
    workers=[asyncio.create_task(worker()) for _ in range(concurrency)]
    run_error=None
    emit('run_start',metadata=metadata or {},planned=len(arrivals),time_scale=float(scale),scenario_type='Trace-Scaled Scenario' if scale!=1 or trace_scaled else 'Original Trace',
        duration_ms=str(duration_ms),anchor_monotonic_ns=anchor_mono,anchor_utc=utc(anchor_utc),concurrency=concurrency,queue_capacity=queue_capacity)
    try:
        for index,record in enumerate(records):
            await wait_until(record['planned_arrival_monotonic_ns'])
            record.update(submitted_monotonic_ns=time.monotonic_ns(),submitted_utc=utc(time.time_ns()),status='submitted')
            try:
                queue.put_nowait(index);peak_queue=max(peak_queue,queue.qsize());record['queue_depth_at_submit']=queue.qsize()
                emit('request_submitted',arrival_id=record['arrival_id'],planned_monotonic_ns=record['planned_arrival_monotonic_ns'],submitted_monotonic_ns=record['submitted_monotonic_ns'],submitted_utc=record['submitted_utc'],queue_depth=queue.qsize())
            except asyncio.QueueFull:terminal(record,'rejected',{'type':'QueueFull','message':'Explicit admission failure; request not silently dropped'})
        # Preserve trailing idle time as part of the declared observation window.
        await wait_until(anchor_mono+int(Decimal(duration_ns)/scale))
        publisher_end=time.monotonic_ns();emit('arrival_window_closed',outstanding=active+queue.qsize(),queue_depth=queue.qsize())
        await queue.join()
    except BaseException as exc:
        run_error={'type':type(exc).__name__,'message':str(exc)};publisher_end=time.monotonic_ns();emit('run_error',**run_error)
        raise
    finally:
        for w in workers:w.cancel()
        await asyncio.gather(*workers,return_exceptions=True)
        for record in records:
            if record['completed_monotonic_ns'] is None:terminal(record,'cancelled',{'type':'RunAborted','message':'No completion before run interruption'})
        counts=Counter(r['status'] for r in records);end=time.monotonic_ns()
        submitted=[r for r in records if r['submitted_monotonic_ns'] is not None]
        started=[r for r in records if r['harness_started_monotonic_ns'] is not None]
        summary={'status':'failed' if run_error else 'complete','error':run_error,'metadata':metadata or {},'planned':len(records),
            'submitted':len(submitted),'accepted':len(submitted)-counts['rejected'],'started':len(started),'terminal_counts':dict(counts),'unaccounted_requests':sum(r['completed_monotonic_ns'] is None for r in records),
            'submission_loss_rate':(len(records)-len(submitted))/len(records) if records else None,
            'failure_or_rejection_rate':sum(c for s,c in counts.items() if s!='succeeded')/len(records) if records else None,
            'time_scale':float(scale),'scenario_type':'Trace-Scaled Scenario' if scale!=1 or trace_scaled else 'Original Trace',
            'planned_duration_ms':float(Decimal(duration_ns)/scale/1000000),'actual_observation_ms':(publisher_end-anchor_mono)/1000000,
            'actual_total_ms':(end-anchor_mono)/1000000,'drain_after_window_ms':max(0,(end-publisher_end)/1000000),
            'peak_active':peak_active,'peak_queue_depth':peak_queue,
            'submission_lateness_ms':stats([(r['submitted_monotonic_ns']-r['planned_arrival_monotonic_ns'])/1000000 for r in submitted]),
            'harness_start_lateness_ms':stats([(r['harness_started_monotonic_ns']-r['planned_arrival_monotonic_ns'])/1000000 for r in started]),
            'queue_wait_to_harness_ms':stats([(r['harness_started_monotonic_ns']-r['submitted_monotonic_ns'])/1000000 for r in started])}
        fields=['arrival_id','original_timestamp_utc','relative_arrival_ms','source_type','source_partition','window_id','planned_arrival_utc','submitted_utc','harness_started_utc','completed_utc','planned_arrival_monotonic_ns','submitted_monotonic_ns','dispatched_monotonic_ns','harness_started_monotonic_ns','completed_monotonic_ns','status','error']
        with (output/'actual_arrivals.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader()
            for record in records:writer.writerow({**record,'error':json.dumps(record['error']) if record['error'] else ''})
        (output/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n');emit('run_end',summary=summary);events.close()
    return summary
