import asyncio,json,time,uuid
from pathlib import Path
from atsuite.runtime import InvocationRequest,create_runtime_adapter


class DivergenceError(RuntimeError):pass


def semantic(result):
    """Only control-significant output; timing and provider IDs are measured anew."""
    if not isinstance(result,dict):raise ValueError('Tool must return structured JSON')
    fields={k:result[k] for k in ('status','error_type','error','snapshot','checks','state_signature','state_names','branch_ref','step_id','inputs') if k in result}
    if 'artifacts' in result:
        fields['artifacts']=[{k:a[k] for k in ('name','signature','comparison')} for a in sorted(result['artifacts'],key=lambda x:x['name'])]
    return fields


class ToolClient:
    def __init__(self,config,recorder,capture=None):
        self.recorder=recorder;self.target_id=config['target_id'];self.uid=uuid.uuid4().hex
        self.adapter=create_runtime_adapter(config['provider'],config['family'])
        self.adapter.connect({'targets':{self.target_id:{'endpoint':config['endpoint'],'family':config['family']}}})
        self.session=self.adapter.open_session(self.adapter.targets[self.target_id],self.uid)
        self.expected={}
        if capture:
            for line in (Path(capture)/'events.jsonl').read_text().splitlines():
                event=json.loads(line)
                if event['kind']=='tool':
                    if event['key'] in self.expected:raise ValueError('Duplicate captured Tool key')
                    self.expected[event['key']]=event
        self.replay=capture is not None;self.consumed=set();self.timeout=config.get('timeout',240)

    async def call(self,name,args,key,wave=None):
        if self.replay:
            expected=self.expected.get(key)
            if expected is None or expected['tool_name']!=name or expected['args']!=args:
                raise DivergenceError('Replay Tool parameters diverged: '+key)
        start=time.perf_counter()
        self.recorder.emit('tool_start',key=key,tool_name=name,args=args,wave=wave)
        request=InvocationRequest(target_id=self.target_id,tool_name=name,args=args,uid=self.uid,
            call_id=uuid.uuid4().hex,session=self.session,timeout=self.timeout,run_id=self.recorder.request_id)
        response=await asyncio.to_thread(self.adapter.invoke,request)
        result=response.result
        self.recorder.emit('tool',key=key,tool_name=name,args=args,wave=wave,result=result,
            transport=response.to_dict(),duration_s=time.perf_counter()-start)
        structured_failure=isinstance(result,dict) and result.get('status') in {'error','timeout'} and result.get('error_type')
        if response.status!='ok' and not structured_failure:raise RuntimeError(f'Tool transport {response.status}: {response.error}')
        if self.replay:
            if semantic(result)!=semantic(expected['result']):
                self.recorder.emit('divergence',key=key,expected=semantic(expected['result']),actual=semantic(result))
                raise DivergenceError('Actual Tool output incompatible with Capture: '+key)
            self.consumed.add(key)
        return result

    def close(self):self.adapter.close_session(self.session)

    def assert_consumed(self):
        if self.replay and self.consumed!=set(self.expected):raise DivergenceError('Replay did not execute every captured Tool call')
