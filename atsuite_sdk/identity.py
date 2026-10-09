"""One collision-resistant state namespace used for save/restore/seed/cleanup."""
from dataclasses import dataclass
import hashlib
import re

def component(value):
    raw=str(value or 'default')
    if re.fullmatch(r'[A-Za-z0-9_-]{1,80}',raw): return raw
    return 'key-'+hashlib.sha256(raw.encode()).hexdigest()[:32]

@dataclass(frozen=True)
class StateKey:
    tenant:str
    experiment:str
    candidate:str
    scenario:str
    domain:str
    session:str
    def path(self):
        return '/'.join(component(v) for v in (self.tenant,self.experiment,self.candidate,self.scenario,self.domain,self.session))+'.json'
