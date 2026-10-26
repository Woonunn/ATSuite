import asyncio
import json
from pathlib import Path


class ReplayModel:
    def __init__(self,capture,recorder):
        if json.loads((Path(capture)/'result.json').read_text()).get('status')!='complete':
            raise ValueError('Only successful Capture traces can be replayed')
        self.events=[json.loads(line) for line in (Path(capture)/'events.jsonl').read_text().splitlines()]
        self.llm=[e for e in self.events if e['kind']=='llm'];self.index=0;self.recorder=recorder

    async def decide(self,phase,iteration,messages):
        if self.index>=len(self.llm):raise ValueError('Replay requested an uncaptured LLM decision')
        event=self.llm[self.index];self.index+=1
        if (event['phase'],event['iteration'])!=(phase,iteration):raise ValueError('LLM control flow diverged from Capture')
        if messages!=event['request']['messages']:raise ValueError('Replay LLM inputs diverged from Capture')
        # Recorded model latency is an explicit replay wait, never CPU computation.
        await asyncio.sleep(event['duration_s'])
        self.recorder.emit('llm',**{k:v for k,v in event.items() if k not in {'seq','kind','utc','monotonic_ns','request_id','session_id','source'}},source='replayed_capture_no_api_charge')
        return event['decision']

    def assert_consumed(self):
        if self.index!=len(self.llm):raise ValueError('Replay did not consume all captured LLM decisions')
