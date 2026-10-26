"""One arrival per sampled Session's earliest explicitly user-initiated LLM call."""
import argparse
from collections import Counter
import csv
from datetime import date,timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import pandas as pd
import yaml
from .prepare_copilot import ROOT,DEFAULT,LOCK,sha

CONFIG=ROOT/'experiments/exp1/configs/traffic.yaml'
ARRIVAL_FIELDS=['arrival_id','original_timestamp_utc','relative_arrival_ms','source_partition','source_type','window_id']
ISO=re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$')


def load_config(path=CONFIG):
    config=yaml.safe_load(Path(path).read_text());config['expected_dates']=[str(d) for d in config['expected_dates']]
    if config['timezone']!='UTC' or config['window_minutes'] not in (10,15,60):raise ValueError('Use UTC windows of 10, 15 or 60 minutes')
    dates=[date.fromisoformat(d) for d in config['expected_dates']]
    if dates!=[dates[0]+timedelta(days=i) for i in range(len(dates))]:raise ValueError('Dates must be consecutive and sorted')
    return config


def timestamp_ns(value):
    if not isinstance(value,str) or not ISO.fullmatch(value):raise ValueError('Expected timezone-qualified ISO timestamp with <=9 fractional digits')
    ts=pd.Timestamp(value)
    if ts.tzinfo is None or pd.isna(ts):raise ValueError('Invalid timestamp')
    return int(ts.tz_convert('UTC').value)


def utc(value):return pd.Timestamp(value,unit='ns',tz='UTC').isoformat().replace('+00:00','Z')


def milliseconds(value):return f'{value//1000000}.{value%1000000:06d}'


def window_id(value,minutes=60):
    if minutes not in (10,15,60):raise ValueError('Unsupported window duration')
    start=pd.Timestamp(value,unit='ns',tz='UTC').floor(f'{minutes}min')
    return start.strftime('copilot-%Y%m%dT%H%M%SZ-')+('PT1H' if minutes==60 else f'PT{minutes}M')


def extract(raw,output,config,lock_path=LOCK):
    raw,output=Path(raw),Path(output);output.mkdir(parents=True,exist_ok=False)
    counts=Counter();reasons=Counter();examples=[];inputs=[];day_records=Counter()
    lock=json.loads(Path(lock_path).read_text())
    locked_archives={a['name'].removeprefix('date.').removesuffix('.tar.gz'):a['digest'].removeprefix('sha256:') for a in lock['assets']}
    start=timestamp_ns(config['expected_dates'][0]+'T00:00:00Z')
    end=timestamp_ns(str(date.fromisoformat(config['expected_dates'][-1])+timedelta(days=1))+'T00:00:00Z')
    policy_hash=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
    report={'status':'processing','policy':config,'policy_sha256':policy_hash,'source_lock_sha256':sha(lock_path),
        'extractor_sha256':sha(Path(__file__)),
        'population':'sampled non-enterprise Copilot Sessions; NOT full product QPS','origin_timestamp_utc':utc(start),'end_timestamp_utc':utc(end)}
    db=sqlite3.connect(output/'sessions.sqlite');db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,n_records INTEGER,first_ns INTEGER,payload TEXT,unknown_ns INTEGER,ambiguous INTEGER)')
    upsert='''INSERT INTO sessions VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        n_records=sessions.n_records+1,
        payload=CASE WHEN sessions.first_ns IS NULL OR excluded.first_ns<sessions.first_ns THEN excluded.payload ELSE sessions.payload END,
        first_ns=CASE WHEN sessions.first_ns IS NULL OR excluded.first_ns<sessions.first_ns THEN excluded.first_ns ELSE sessions.first_ns END,
        unknown_ns=CASE WHEN sessions.unknown_ns IS NULL OR excluded.unknown_ns<sessions.unknown_ns THEN excluded.unknown_ns ELSE sessions.unknown_ns END,
        ambiguous=MAX(sessions.ambiguous,excluded.ambiguous)'''
    def reject(reason,session=None,source=None):
        reasons[reason]+=1
        if len(examples)<100:examples.append({'reason':reason,'session_id':session,'source':source})
    try:
        for day in config['expected_dates']:
            partition=raw/f'date={day}';marker=partition/'_verified.json'
            if not marker.exists():raise ValueError('Partition missing or unverified: '+day)
            verification=json.loads(marker.read_text());manifest=json.loads((partition/'manifest.json').read_text())
            if verification['archive_sha256']!=locked_archives.get(day):raise ValueError('Partition archive does not match pinned release asset')
            files=sorted(partition.glob('*.jsonl.gz'))
            if not files:raise ValueError('Empty source partition')
            if manifest.get('complete') is not True or manifest.get('date')!=day or manifest.get('n_shards')!=len(files):raise ValueError('Incomplete official partition manifest')
            shard_records={f"shard-{entry['shard_idx']:04d}.jsonl.gz":entry for entry in manifest['shards']}
            if set(shard_records)!={p.name for p in files}:raise ValueError('Official shard list mismatch')
            recorded={entry['name']:entry for entry in verification['files']}
            if {p.name for p in partition.iterdir() if p.name!='_verified.json'}!=set(recorded):raise ValueError('Changed partition file set')
            for name,entry in recorded.items():
                if sha(partition/name)!=entry['sha256']:raise ValueError('Source checksum mismatch: '+name)
            inputs.append({'partition':partition.name,'verification_sha256':sha(marker),'manifest_sha256':sha(partition/'manifest.json'),'shards':len(files),'source_manifest':manifest})
            partition_start=timestamp_ns(day+'T00:00:00Z');grace=int(config['partition_grace_hours'])*3600*10**9
            for path in files:
                source=str(path.relative_to(raw))
                if path.stat().st_size!=shard_records[path.name]['size_bytes']:raise ValueError('Official shard size mismatch')
                line_count=0
                with gzip.open(path,'rt',encoding='utf-8') as stream:
                    for lineno,line in enumerate(stream,1):
                        counts['source_records']+=1;day_records[day]+=1;line_count+=1
                        try:record=json.loads(line)
                        except (ValueError,TypeError):reject('invalid_json',source=source+':'+str(lineno));continue
                        if not isinstance(record,dict):reject('invalid_record',source=source);continue
                        sid=record.get('session_id')
                        if not isinstance(sid,str) or not sid.strip():reject('missing_session_id',source=source);continue
                        bad=record.get('date')!=day
                        turns=record.get('turns');first=None;unknown=None;ambiguous=int(bad)
                        if bad:reject('record_partition_date_mismatch',sid,source)
                        if not isinstance(turns,list):turns=[];ambiguous=1;reject('invalid_turns',sid,source)
                        for ti,turn in enumerate(turns):
                            if not isinstance(turn,dict):ambiguous=1;reject('invalid_turn',sid,source);continue
                            counts['turns']+=1;tid=turn.get('turn_id');calls=turn.get('llm_calls')
                            if not isinstance(calls,list):ambiguous=1;reject('invalid_llm_calls',sid,source);continue
                            for ci,call in enumerate(calls):
                                counts['llm_calls']+=1
                                if not isinstance(call,dict):ambiguous=1;reject('invalid_llm_call',sid,source);continue
                                marker_value=call.get('initiator_type');kind=marker_value.strip().lower() if isinstance(marker_value,str) else None
                                if kind not in ('user','agent'):reject('missing_or_invalid_initiator',sid,source);kind=None
                                else:counts[kind+'_llm_calls']+=1
                                if marker_value!=kind and kind is not None:counts['normalized_initiator_labels']+=1
                                try:ns=timestamp_ns(call.get('timestamp'))
                                except (ValueError,TypeError,OverflowError):
                                    reject('invalid_timestamp',sid,source)
                                    if kind!='agent':ambiguous=1
                                    continue
                                duration=call.get('duration_ms')
                                if isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<0:
                                    reject('missing_or_invalid_duration',sid,source);duration=None
                                if kind is None:unknown=ns if unknown is None else min(unknown,ns)
                                if kind!='user':continue
                                point={'session_id':sid,'turn_id':tid,'llm_timestamp_raw':call['timestamp'],'llm_timestamp_ns':ns,
                                    'duration_ms':duration,'source_partition':partition.name,'source_shard':source,'source_line':lineno,
                                    'turn_index':ti,'llm_call_index':ci,'cross_date_anomaly':not(partition_start-grace<=ns<partition_start+86400*10**9+grace)}
                                if first is None or ns<first['llm_timestamp_ns']:first=point
                        db.execute(upsert,(sid,1,first['llm_timestamp_ns'] if first else None,json.dumps(first) if first else None,unknown,ambiguous))
                        if counts['source_records']%1000==0:db.commit()
                if line_count!=shard_records[path.name]['n_sessions']:raise ValueError('Official shard record count mismatch')
                print(day,path.name,'parsed',flush=True)
            db.commit()
            if day_records[day]!=manifest['n_sessions']:raise ValueError('Official partition record count mismatch')
        counts['distinct_sessions']=db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        counts['duplicate_session_records']=db.execute('SELECT COALESCE(SUM(n_records-1),0) FROM sessions').fetchone()[0]
        excluded=Counter()
        with (output/'arrivals.csv').open('w',newline='') as out,(output/'arrival_sources.csv').open('w',newline='') as audit:
            writer=csv.DictWriter(out,fieldnames=ARRIVAL_FIELDS);writer.writeheader()
            fields=['arrival_id','session_id','turn_id','llm_timestamp_raw','llm_timestamp_ns','duration_ms','source_partition','source_shard','source_line','turn_index','llm_call_index']
            provenance=csv.DictWriter(audit,fieldnames=fields);provenance.writeheader()
            for sid,ns,payload,unknown,ambiguous in db.execute('SELECT id,first_ns,payload,unknown_ns,ambiguous FROM sessions ORDER BY first_ns,id'):
                point=json.loads(payload) if payload else None
                reason='ambiguous_first_user_call' if ambiguous else 'no_explicit_user_call' if ns is None else 'unknown_initiator_at_or_before_first_user' if unknown is not None and unknown<=ns else 'first_user_missing_turn_id' if not isinstance(point['turn_id'],str) or not point['turn_id'].strip() else 'first_user_cross_date_anomaly' if point['cross_date_anomaly'] else 'first_user_outside_observation_week' if not start<=ns<end else None
                if reason:excluded[reason]+=1;continue
                identifier=hashlib.sha256(('ghcp-coding-agent-2026:'+sid).encode()).hexdigest()
                writer.writerow({'arrival_id':identifier,'original_timestamp_utc':utc(ns),'relative_arrival_ms':milliseconds(ns-start),
                    'source_partition':point['source_partition'],'source_type':config['source_type'],'window_id':window_id(ns,config['window_minutes'])})
                provenance.writerow({'arrival_id':identifier,**{k:point[k] for k in fields if k!='arrival_id'}})
                counts['arrivals']+=1
        assert counts['arrivals']+sum(excluded.values())==counts['distinct_sessions']
        report.update(status='complete',counts=dict(counts),field_cleaning_counts=dict(reasons),excluded_sessions=dict(excluded),
            partition_record_counts=dict(day_records),diagnostic_examples=examples,inputs=inputs,
            arrivals_sha256=sha(output/'arrivals.csv'),provenance_sha256=sha(output/'arrival_sources.csv'),
            duration_policy='Missing/invalid duration is null, never zero-filled; it does not change a known arrival timestamp.',
            first_user_policy='Globally earliest user call per session, across all records/partitions; invalid/ambiguous earlier user initiation excludes the Session rather than promoting a later turn.')
        (output/'cleaning_report.json').write_text(json.dumps(report,indent=2)+'\n')
        return report
    except BaseException as exc:
        report.update(status='failed',error_type=type(exc).__name__,error=str(exc),counts=dict(counts),field_cleaning_counts=dict(reasons))
        (output/'cleaning_report.json').write_text(json.dumps(report,indent=2)+'\n')
        (output/'arrivals.csv').unlink(missing_ok=True);raise
    finally:db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--raw',type=Path,default=DEFAULT/'raw');p.add_argument('--output',type=Path,required=True);p.add_argument('--config',type=Path,default=CONFIG)
    a=p.parse_args();print(json.dumps(extract(a.raw,a.output,load_config(a.config))['counts']))
