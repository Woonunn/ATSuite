"""Describe sampled Session arrivals and select windows without platform measurements."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
from experiments.exp1.data.extract_copilot_arrivals import load_config,CONFIG,timestamp_ns,utc,window_id
from experiments.exp1.data.prepare_copilot import sha


def distribution(values):
    x=np.asarray(values,dtype=float)
    if not len(x):return {'count':0,'mean':None,'std':None,'cv':None,'burstiness':None,'quantiles':None}
    mean=float(x.mean());std=float(x.std());denom=std+mean
    return {'count':len(x),'mean':mean,'std':std,'cv':std/mean if mean>0 else None,
        'burstiness':(std-mean)/denom if denom>0 else None,
        'quantiles':{str(q):float(np.quantile(x,q)) for q in (0,.5,.9,.95,.99,1)}}


def analyze(arrivals,output,config):
    arrivals,output=Path(arrivals),Path(output);output.mkdir(parents=True,exist_ok=False)
    cleaning=json.loads(arrivals.with_name('cleaning_report.json').read_text())
    if cleaning['status']!='complete' or sha(arrivals)!=cleaning['arrivals_sha256']:raise ValueError('Arrivals do not match completed cleaning report')
    if hashlib.sha256(json.dumps(cleaning['policy'],sort_keys=True).encode()).hexdigest()!=cleaning['policy_sha256']:raise ValueError('Cleaning policy checksum mismatch')
    # Window length is independent of timestamp cleaning; retain the original arrival table.
    if {k:v for k,v in config.items() if k!='window_minutes'}!={k:v for k,v in cleaning['policy'].items() if k!='window_minutes'}:
        raise ValueError('Only window length may change without repeating extraction')
    selection_sha256=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
    window_minutes=config['window_minutes'];window_seconds=window_minutes*60
    data=pd.read_csv(arrivals,dtype={'relative_arrival_ms':str})
    if data.arrival_id.duplicated().any():raise ValueError('Duplicate arrival ID')
    ns=pd.to_datetime(data.original_timestamp_utc,utc=True,format='ISO8601').astype('int64').to_numpy()
    if len(ns)>1 and np.any(np.diff(ns)<0):raise ValueError('Arrivals are not ordered')
    origin=timestamp_ns(cleaning['origin_timestamp_utc']);end=timestamp_ns(cleaning['end_timestamp_utc']);seconds=(end-origin)//10**9
    if len(ns) and (ns.min()<origin or ns.max()>=end):raise ValueError('Arrival outside observation span')
    per_second=np.bincount(((ns-origin)//10**9).astype(int),minlength=seconds)
    dates=pd.date_range(pd.Timestamp(origin,tz='UTC'),periods=seconds,freq='s')
    series=pd.Series(per_second,index=dates)
    per_minute=series.resample('min').sum();per_hour=series.resample('h').sum()
    per_minute.rename('sampled_session_arrivals').to_csv(output/'per_minute.csv',index_label='minute_utc')
    per_hour.rename('sampled_session_arrivals').to_csv(output/'per_hour.csv',index_label='hour_utc')
    gaps=np.diff(ns)/10**6;pd.DataFrame({'interarrival_ms':gaps}).to_csv(output/'interarrival_ms.csv',index=False)
    gap_summary=distribution(gaps);window_rows=[]
    for start,count in series.resample(f'{window_minutes}min').sum().items():
        hour=series.loc[start:start+pd.Timedelta(minutes=window_minutes)-pd.Timedelta(seconds=1)]
        minutes=per_minute.loc[start:start+pd.Timedelta(minutes=window_minutes)-pd.Timedelta(minutes=1)]
        if len(hour)!=window_seconds:raise ValueError('Partial candidate window')
        mean=float(count)/window_seconds
        window_rows.append({'window_id':window_id(start.value,window_minutes),'start_utc':utc(start.value),'end_utc':utc(start.value+window_seconds*10**9),
            'duration_ms':window_seconds*1000,'arrival_count':int(count),'sampled_mean_arrivals_per_s':mean,
            'sampled_peak_arrivals_per_s':int(hour.max()),'peak_to_mean':float(hour.max())/mean if mean>0 else None,
            'peak_per_minute':int(minutes.max()),'minute_fano_factor':float(minutes.var(ddof=0)/minutes.mean()) if minutes.mean()>0 else None})
    windows=pd.DataFrame(window_rows);windows.to_csv(output/'windows.csv',index=False)
    ordered=sorted(window_rows,key=lambda w:(w['arrival_count'],w['start_utc']))
    selection=config['selection'];selected={}
    if selection['rank_rule']!='floor(q * (N - 1))' or selection['ties']!='earlier_window_utc' or not selection['include_zero_windows']:
        raise ValueError('Unrecognized predeclared selection rule')
    for role,q in selection['quantiles'].items():
        if not 0<=q<=1:raise ValueError('Invalid quantile')
        rank=int(np.floor(q*(len(ordered)-1)));selected[role]={**ordered[rank],'rank_zero_based':rank,'quantile':q}
    daily=series.resample('D').sum();daily_frame=pd.DataFrame({'date_utc':daily.index.strftime('%Y-%m-%d'),'sampled_arrivals':daily.values,
        'day_type':['weekend' if t.weekday()>=5 else 'weekday' for t in daily.index]})
    daily_frame.to_csv(output/'per_day.csv',index=False)
    groups={}
    for kind in ('weekday','weekend'):
        counts=daily_frame.loc[daily_frame.day_type==kind,'sampled_arrivals'].to_numpy()
        groups[kind]={'observed_days':len(counts),'sampled_arrivals':int(counts.sum()),'mean_per_day':float(counts.mean()) if len(counts) else None}
    summary={'population':cleaning['population'],'warning':'These are sampled Session arrival rates, not complete Copilot product QPS; one observed week does not establish seasonal behavior.',
        'arrivals':len(data),'origin_timestamp_utc':utc(origin),'end_timestamp_utc':utc(end),'duration_ms':seconds*1000,
        'arrivals_sha256':sha(arrivals),'arrivals_path':str(arrivals.resolve()),'selection_policy_sha256':selection_sha256,'selection_rule':selection,
        'source_cleaning_policy_sha256':cleaning['policy_sha256'],'source_arrival_window_minutes':cleaning['policy']['window_minutes'],
        'window_minutes':window_minutes,'candidate_window_count':len(window_rows),
        'selected_windows':selected,'interarrival_ms':gap_summary,'timestamp_ties':int(np.sum(np.diff(ns)==0)),
        'sampled_mean_arrivals_per_s':len(data)/seconds,'per_minute':distribution(per_minute.values),
        'per_hour':distribution(per_hour.values),'minute_fano_factor':float(per_minute.var(ddof=0)/per_minute.mean()) if per_minute.mean()>0 else None,
        'weekday_weekend':groups}
    (output/'traffic_summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
    # Exported figures are analysis artifacts, not an online dashboard or measured workload.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(3,1,figsize=(12,10),constrained_layout=True)
    per_minute.plot(ax=axes[0],linewidth=.6);axes[0].set_ylabel('Sampled Sessions/minute');axes[0].set_xlabel('UTC')
    per_hour.plot(ax=axes[1]);axes[1].set_ylabel('Sampled Sessions/hour');axes[1].set_xlabel('UTC')
    if len(gaps):
        sorted_gaps=np.sort(gaps);axes[2].plot(sorted_gaps,np.arange(1,len(gaps)+1)/len(gaps));axes[2].set_xscale('symlog',linthresh=1)
    axes[2].set_xlabel('Original interarrival time (ms, zero ties retained)');axes[2].set_ylabel('CDF')
    fig.suptitle('Copilot sampled first-user Session arrivals (not full product QPS)')
    fig.savefig(output/'traffic.png',dpi=150);fig.savefig(output/'traffic.pdf');plt.close(fig)
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--arrivals',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--config',type=Path,default=CONFIG)
    p.add_argument('--window-minutes',type=int,choices=[10,15,60]);a=p.parse_args();config=load_config(a.config)
    if a.window_minutes is not None:config['window_minutes']=a.window_minutes
    r=analyze(a.arrivals,a.output,config);print(json.dumps({'arrivals':r['arrivals'],'selected_windows':r['selected_windows']}))
