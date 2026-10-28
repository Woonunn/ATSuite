"""Unify scheduler, real Tool events, and delayed provider evidence; unknown is null."""
import argparse
import csv
from datetime import datetime,timezone
import json
from pathlib import Path
import time
import uuid
from experiments.exp1.data.prepare_e1a import ROOT,sha
from experiments.exp1.runner.arrival_scheduler import stats
from atsuite.analysis.collectors import create_collector
from atsuite.analysis.model import RunContext,NodeObservation,InvocationObservation,SessionObservation


def interval_union(intervals):
    total=0;end=None
    for a,b in sorted(intervals):
        if b<a:raise ValueError('Negative execution interval')
        total+=max(0,b-max(a,end if end is not None else a));end=max(end if end is not None else b,b)
    return total


def parallel_peak(intervals):
    active=peak=0
    for _,change in sorted([(a,1) for a,b in intervals if b>a]+[(b,-1) for a,b in intervals if b>a]):
        active+=change;peak=max(peak,active)
    return peak


def known_sum(values):return sum(values) if values and all(v is not None for v in values) else None


def read_events(path):return [json.loads(line) for line in Path(path).read_text().splitlines()] if Path(path).exists() else []


def request_metrics(row,terminal,directory):
    events=read_events(directory/'events.jsonl');tools=[e for e in events if e['kind']=='tool']
    starts={e['key']:e for e in events if e['kind']=='tool_start'}
    client=[(starts[e['key']]['monotonic_ns']/1e6,e['monotonic_ns']/1e6) for e in tools if e['key'] in starts]
    results=[e['result'] for e in tools if isinstance(e.get('result'),dict)]
    analyses=[e['result'] for e in tools if e['tool_name']=='execute_analysis' and isinstance(e.get('result'),dict)]
    # A shared MCP environment has a common monotonic clock. Independent FaaS instances do not.
    domains={r.get('clock_domain') or r.get('container_name') for r in analyses}
    same_clock=len(domains)==1 and bool(analyses) and None not in domains
    intervals=[(r['exec_started_monotonic'],r['exec_ended_monotonic']) for r in analyses
               if r.get('exec_started_monotonic') is not None and r.get('exec_ended_monotonic') is not None]
    peak=parallel_peak(intervals) if same_clock and len(intervals)==len(analyses) else None
    sdk_sync=[e['transport'].get('provider_metadata',{}).get('state_sync',{}).get('total_ms') for e in tools]
    compute=[(r['exec_ended_monotonic']-r['exec_started_monotonic'])*1000 for r in results if 'exec_ended_monotonic' in r]
    usage=[r['worker_usage'] for r in results if 'worker_usage' in r]
    end=terminal.get('completed_monotonic_ns');arrival=terminal.get('planned_arrival_monotonic_ns');start=terminal.get('harness_started_monotonic_ns');submitted=terminal.get('submitted_monotonic_ns')
    status=terminal['status']
    if status=='failed' and any(e.get('transport',{}).get('status')=='timeout' for e in tools):status='timeout'
    return {**row,'status':status,'request_arrival_utc':terminal.get('planned_arrival_utc'),'request_start_utc':terminal.get('harness_started_utc'),
        'request_end_utc':terminal.get('completed_utc'),'end_to_end_ms':(end-arrival)/1e6 if end is not None and arrival is not None else None,
        'harness_latency_ms':(end-start)/1e6 if end is not None and start is not None else None,
        'harness_queue_ms':(start-submitted)/1e6 if start is not None and submitted is not None else None,
        'active_tool_path_ms':interval_union(client) if client else None,
        'tool_execution_ms':known_sum([e['transport'].get('execution_elapsed_ms') for e in tools]),
        'worker_compute_ms':sum(compute) if compute else None,'state_synchronization_ms':known_sum(sdk_sync),
        'tool_parallelism':peak,'parallelism_clock_basis':'common environment monotonic clock' if same_clock else 'unavailable across independent provider clocks; RPC concurrency is separate',
        'rpc_parallelism':parallel_peak(client) if client else None,
        'worker_peak_rss_bytes':max(u['peak_rss_bytes'] for u in usage) if usage else None,
        'worker_cpu_seconds':known_sum([u.get('cpu_seconds') for u in usage]),
        'application_cold_start':next((r['execution_environment']['first_task_in_process'] for r in results if 'execution_environment' in r),None),
        'execution_environment':next((r['execution_environment'] for r in results if 'execution_environment' in r),None),
        'peak_memory_bytes':None,'cpu_usage':{'worker_average_cores':known_sum([u.get('cpu_seconds') for u in usage])/(sum(compute)/1000) if usage and compute else None,'scope':'measured worker computation; excludes process startup and SDK CPU'},'cold_start':None,'provider_queue_ms':None,'provider_schedule_ms':None,
        'provider_limits':next((r['provider_limits'] for r in results if 'provider_limits' in r),None),'tool_calls':len(tools),'tool_metric_coverage':len(usage),'cost_breakdown':None,'cost_status':'pending_vendor_billing',
        'error':terminal.get('error'),'graph_error':next((e.get('error') for e in reversed(events) if e['kind']=='run_error'),None)}


def collect_local(run):
    run=Path(run);info=json.loads((run/'run.json').read_text());workload=json.loads((Path(info['workload'])/'manifest.json').read_text())
    rows=list(csv.DictReader((run/'planned_requests.csv').open()));events=read_events(run/'schedule/events.jsonl')
    terminal_rows=[e for e in events if e['kind']=='request_terminal']
    terminals={e['arrival_id']:e for e in terminal_rows}
    if len(terminal_rows)!=len(terminals):raise ValueError('Duplicate scheduler terminal records')
    if set(terminals)!={r['arrival_id'] for r in rows}:raise ValueError('Scheduled requests are missing terminal records')
    records=[request_metrics(r,terminals[r['arrival_id']],run/'requests'/r['arrival_id']) for r in rows]
    for record in records:record.update(provider=info['provider'],pilot=info['pilot'])
    slo=workload['config']['slo_ms'];n=len(records);success=[r for r in records if r['status']=='succeeded']
    summary={'provider':info['provider'],'pilot':info['pilot'],'sequence_seed':info['sequence_seed'],'window_role':info['window_role'],
        'request_table_sha256':info['request_table_sha256'],'planned_requests_sha256':sha(run/'planned_requests.csv'),'requests':n,'successes':len(success),'success_rate':len(success)/n if n else None,
        'slo_ms':slo,'slo_attainment':sum(r['end_to_end_ms']<=slo for r in success)/n if n else None,
        'latency_ms_success_only':stats([r['end_to_end_ms'] for r in success]),'end_to_end_ms_all_terminal':stats([r['end_to_end_ms'] for r in records]),
        'arrival_rate_per_s':n/(float(info['duration_ms'])/1000),'window_duration_ms':info['duration_ms'],
        'completion_throughput_per_s':None,'observed_window_goodput_per_s':None,
        'active_tool_path_ms':stats([r['active_tool_path_ms'] for r in success if r['active_tool_path_ms'] is not None]),
        'state_sync_ms':stats([r['state_synchronization_ms'] for r in success if r['state_synchronization_ms'] is not None]),
        'cost_status':'pending_vendor_billing','cost_breakdown':None,'total_cost':None,'currency':None,'total_cost_cny':None,
        'metric_coverage':{key:sum(r[key] is not None for r in records) for key in ('peak_memory_bytes','worker_peak_rss_bytes','worker_cpu_seconds','cold_start','state_synchronization_ms','provider_queue_ms','tool_parallelism')},
        'cost_note':'No missing metrics or absent bill lines are replaced with zero. Tool usage is not an invoice.'}
    schedule=json.loads((run/'schedule/summary.json').read_text());summary['completion_throughput_per_s']=len(success)/(schedule['actual_total_ms']/1000)
    window_end=next(e['logged_monotonic_ns'] for e in events if e['kind']=='arrival_window_closed')
    completed_in_window=sum(1 for r in success if terminals[r['arrival_id']]['completed_monotonic_ns']<=window_end)
    summary['observed_window_goodput_per_s']=completed_in_window/(float(info['duration_ms'])/1000)
    with (run/'request_metrics.jsonl').open('w') as f:
        for record in records:f.write(json.dumps(record,allow_nan=False)+'\n')
    (run/'window_report.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n');return summary


def provider_observations(run):
    run=Path(run);info=json.loads((run/'run.json').read_text());provider=info['provider'];entry=info['deployment'];actual=entry['actual']
    endpoint_map=json.loads((ROOT/'url_results'/Path(entry['config']).name).read_text());target_id,target=next(iter(endpoint_map['targets'].items()))
    observability={'aws_lambda':'aws_lambda_cloudwatch','aws_agentcore':'aws_agentcore_cloudwatch','ali_fc':'ali_sls','ali_agentrun':'ali_sls'}[provider]
    schedule=read_events(run/'schedule/events.jsonl');end=max(e['logged_utc'] for e in schedule)
    context=RunContext(uid=run.name,provider=provider,observability_provider=observability,benchmark='DataSciBench',trace=json.loads((Path(info['workload'])/'manifest.json').read_text())['task_id'],family=target['family'],
        config_path=entry['config'],run_id=run.name,start_time=info['started_epoch'],end_time=datetime.fromisoformat(end.replace('Z','+00:00')).timestamp(),
        targets={target_id:{'name':actual['runtime_name'],'runtime':target['resources']}},endpoint_map=endpoint_map)
    nodes={};invocations=[];sessions=[];index=0
    for directory in sorted((run/'requests').iterdir()):
        events=read_events(directory/'events.jsonl');starts={e['key']:e for e in events if e['kind']=='tool_start'};session_events=[e for e in events if e['kind']=='tool_session_open'];session_uid=session_events[0]['uid'] if session_events else directory.name
        session_ids=set()
        for event in events:
            if event['kind']!='tool':continue
            index+=1;transport=event['transport'];meta=transport.get('provider_metadata',{});sid=meta.get('session_id') or ''
            if sid:session_ids.add(sid)
            nodes[index]=NodeObservation(node_id=index,node_name=event['key'],node_type='tool_use',runtime_name=actual['runtime_name'],target_id=target_id,family=target['family'],runtime_config=target['resources'])
            start=starts.get(event['key'],event)
            invocations.append(InvocationObservation(node_id=index,node_name=event['key'],target_id=target_id,runtime_name=actual['runtime_name'],family=target['family'],tool_name=event['tool_name'],
                call_id=transport['request_id'],uid=session_uid,status=transport['status'],error=transport.get('error',''),provider_request_id=transport.get('provider_request_id',''),provider_session_id=sid,
                client_start_time=datetime.fromisoformat(start['utc']).timestamp(),client_elapsed_ms=transport['client_elapsed_ms'],provider_metadata=meta))
        for sid in session_ids:sessions.append(SessionObservation(uid=session_uid,target_id=target_id,runtime_name=actual['runtime_name'],provider_session_id=sid,opened_at=context.start_time,closed_at=context.end_time))
    return context,nodes,invocations,sessions


def enrich_reports(run, metrics):
    run=Path(run)
    if not (run/'request_metrics.jsonl').exists():collect_local(run)
    records=read_events(run/'request_metrics.jsonl')
    by_call={m['invocation_call_id']:m['fields'] for m in metrics if m.get('invocation_call_id')}
    for record in records:
        tools=[e for e in read_events(run/'requests'/record['arrival_id']/'events.jsonl') if e['kind']=='tool']
        fields=[by_call.get(e['transport']['request_id'],{}) for e in tools]
        def number(value):
            try:return float(value) if value is not None else None
            except (ValueError,TypeError):return None
        memory=[number(f.get('memory_usage_mb')) for f in fields]
        cold=[{'true':True,'false':False,'1':True,'0':False}.get(str(f.get('is_cold_start')).lower()) for f in fields]
        record['peak_memory_bytes']=max(memory)*1024**2 if memory and all(m is not None for m in memory) else None
        record['cold_start']=True if True in cold else False if cold and None not in cold else None
        record['provider_schedule_ms']=known_sum([number(f.get('schedule_latency_ms')) for f in fields])
        record['provider_queue_ms']=known_sum([number(f.get('queue_delay_ms')) for f in fields])
        record['provider_tool_measurements']=fields
        record['provider_metric_coverage']=sum(bool(f) for f in fields)
    (run/'request_metrics.jsonl').write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in records))
    with (run/'request_metrics.csv').open('w',newline='') as f:
        flat=[{k:json.dumps(v,sort_keys=True) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in records]
        writer=csv.DictWriter(f,fieldnames=list(flat[0]));writer.writeheader();writer.writerows(flat)
    report=json.loads((run/'window_report.json').read_text())
    for key in report['metric_coverage']:report['metric_coverage'][key]=sum(r[key] is not None for r in records)
    report['provider_usage_status']='collected; usage is not an invoice'
    (run/'window_report.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')


def info_region(run):return json.loads((Path(run)/'run.json').read_text())['deployment']['actual']['region']


def collect_provider(run, *, allow_early=False):
    run=Path(run);context,nodes,invocations,sessions=provider_observations(run)
    due=context.end_time+(1800 if context.provider.startswith('aws_') else 120)
    if time.time()<due and not allow_early:
        pending={'status':'waiting_for_ingestion','not_before_utc':datetime.fromtimestamp(due,timezone.utc).isoformat(),'seconds_remaining':due-time.time()}
        (run/'provider_collection_status.json').write_text(json.dumps(pending,indent=2)+'\n');return pending
    collector=create_collector(context.observability_provider)
    if context.provider.startswith('aws_'):
        from experiments.exp1.runner.provider_usage import BatchCloudWatch
        collector._cloudwatch=BatchCloudWatch(info_region(run))
    if context.provider.startswith('ali_'):
        from experiments.exp1.runner.provider_usage import collect_ali
        collection=collect_ali(context,invocations)
    else:collection=collector.collect(context,nodes=nodes,invocations=invocations,sessions=sessions)
    # Older collectors expose zero-valued session usage for empty log groups; remove that inference.
    metrics=[]
    for metric in collection.metrics:
        value=metric.to_dict()
        if metric.source=='agentcore_usage' and not metric.fields.get('log_entries'):
            value['fields']={**metric.fields,'vcpu_hours':None,'memory_gb_hours':None}
        metrics.append(value)
    if context.provider=='aws_agentcore':
        unknown=[m['fields']['session_id'] for m in metrics if m['source']=='agentcore_usage' and (m['fields'].get('vcpu_hours') is None or m['fields'].get('memory_gb_hours') is None)]
        if unknown:collection.diagnostics.append({'kind':'session_usage_pending_or_unavailable','session_ids':unknown,'note':'Delivery can lag up to 60 minutes; absent fields are not zero usage'})
    result={'status':'collected','collected_epoch':time.time(),'metrics':metrics,'diagnostics':collection.diagnostics,
            'evidence':[e.to_dict() for e in collection.evidence],'cost_status':'usage_evidence_only_not_an_invoice'}
    prefix='provider_evidence-early' if allow_early and time.time()<due else 'provider_evidence'
    path=run/(prefix+'-'+str(time.time_ns())+'-'+uuid.uuid4().hex[:8]+'.json')
    path.write_text(json.dumps(result,indent=2,default=str,allow_nan=False)+'\n')
    (run/'provider_collection_status.json').write_text(json.dumps({'status':'collected','path':str(path.resolve().relative_to(ROOT)),'sha256':sha(path)},indent=2)+'\n')
    if not (allow_early and time.time()<due):enrich_reports(run,metrics)
    return {'status':'collected','path':str(path),'metrics':len(metrics),'diagnostics':len(collection.diagnostics)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run',type=Path);p.add_argument('--provider',action='store_true');p.add_argument('--allow-early-diagnostic',action='store_true')
    a=p.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    print(json.dumps(collect_provider(a.run,allow_early=a.allow_early_diagnostic) if a.provider else collect_local(a.run),indent=2))
