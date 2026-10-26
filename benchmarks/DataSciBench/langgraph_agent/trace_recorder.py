import json
from pathlib import Path
import threading
import time
from datetime import datetime, timezone


class TraceRecorder:
    def __init__(self,directory,request_id,session_id):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=False)
        self.request_id=request_id;self.session_id=session_id;self.lock=threading.Lock();self.events=[]

    def emit(self,kind,**values):
        with self.lock:
            event={'seq':len(self.events),'kind':kind,'request_id':self.request_id,'session_id':self.session_id,
                   'utc':datetime.now(timezone.utc).isoformat(),'monotonic_ns':time.perf_counter_ns(),**values}
            with (self.directory/'events.jsonl').open('a') as f:f.write(json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n')
            self.events.append(event)
        return event

    def finish(self,state):
        (self.directory/'result.json').write_text(json.dumps(state,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        export_atg(self.events,self.directory/'trace.atg.json')


def export_atg(events,path):
    """ATSuite nodes retain partial-order edges; parallel calls never become a chain."""
    nodes=[{'id':0,'name':'start','type':'start','edge_to':[],'time':0,'output':None}]
    previous=[0];calls=[]
    for event in events:
        if event['kind']=='wave_start':calls=[]
        if event['kind'] not in {'llm','tool'}:continue
        index=len(nodes);node={'id':index,'name':event.get('tool_name',event.get('phase','llm')),
            'type':'llm' if event['kind']=='llm' else 'tool_use','edge_to':[],
            'time':event['duration_s']*1000,'output':event.get('result',event.get('response'))}
        nodes.append(node)
        parents=previous
        if event.get('wave') is not None:
            if not calls:wave_parents=previous[:]
            parents=wave_parents;calls.append(index)
        for parent in parents:
            nodes[parent]['edge_to'].append({'id':index,'params':{'input':event.get('args',event.get('request',{}))},'interval':0})
        previous=calls[:] if event.get('wave') is not None else [index]
    end=len(nodes);nodes.append({'id':end,'name':'end','type':'end','edge_to':[],'time':0,'output':None})
    for parent in previous:nodes[parent]['edge_to'].append({'id':end,'params':{},'interval':0})
    Path(path).write_text(json.dumps({'name':'DataSciBench LangGraph','discription':'Capture partial-order ATG; use LangGraph replay for output consistency enforcement',
        'deploy_config':'','nodes':nodes},ensure_ascii=False,indent=2)+'\n')
