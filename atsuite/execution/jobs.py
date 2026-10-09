from __future__ import annotations
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable
from atsuite.recommender.search import SearchOrchestrator

@dataclass
class ExperimentJob:
    store: Any
    experiment_id: str
    handlers: dict[str, Callable]

    def run(self):
        owner=f'worker-{os.getpid()}-{threading.get_ident()}'
        token=self.store.claim(self.experiment_id,owner)
        if token is None:
            return {**self.store.get(self.experiment_id),'worker_status':'not_claimed'}
        stop=threading.Event()
        def heartbeat():
            while not stop.wait(30):
                try: self.store.renew(self.experiment_id,token)
                except Exception: return
        thread=threading.Thread(target=heartbeat,daemon=True); thread.start()
        orchestrator=SearchOrchestrator(self.store)
        def transition(state,**updates):
            return self.store.transition(self.experiment_id,state,lease_token=token,**updates)
        def cleanup(terminal,reason,updates=None):
            record=self.store.get(self.experiment_id)
            if record['state']!='CLEANUP_PENDING':
                record=transition('CLEANUP_PENDING',cleanup_terminal_state=terminal,cleanup_reason=reason)
            try:
                handler=self.handlers.get('CLEANUP')
                if not handler and (record.get('cleanup_required') or record.get('deployment_records')):
                    raise RuntimeError('cleanup handler required for deployed resources')
                record['_lease_token']=token
                result=(handler(record) if handler else {'cleanup_status':'not_needed'}) or {}
                if result.get('cleanup_status','success') not in {'success','not_needed'}:
                    raise RuntimeError('remote cleanup remains pending')
                return transition(terminal,**{**(updates or {}),**result,'cleanup_status':result.get('cleanup_status','success')})
            except Exception as exc:
                return transition('FAILED',**{**(updates or {}),'cleanup_status':'failed','cleanup_error':f'{type(exc).__name__}: {exc}',
                    'error':(updates or {}).get('error','cleanup failed before completion')})
        try:
            record=self.store.get(self.experiment_id)
            if record['state']=='CLEANUP_PENDING':
                return cleanup(record.get('cleanup_terminal_state','FAILED'),'resume')
            stages=('PREFLIGHT','BUILDING','DEPLOYING','EXPLORING','CONFIRMING','ANALYZING')
            # Legacy injected handler sets remain usable for contract tests; default factory includes PREFLIGHT.
            if 'PREFLIGHT' not in self.handlers: stages=stages[1:]
            for stage in stages:
                record=self.store.get(self.experiment_id)
                if record['state']=='CANCEL_REQUESTED': return cleanup('CANCELLED','cancelled')
                if stage in record.get('completed_stages',[]): continue
                # A resumed record may already be in this stage.
                record=transition(stage)
                self.store.checkpoint(self.experiment_id,stage,{},lease_token=token,status='running')
                handler=self.handlers.get(stage)
                if handler is None: raise RuntimeError(f'missing execution handler for stage {stage}')
                record=self.store.get(self.experiment_id); record['_lease_token']=token
                updates=handler(record)
                if updates is None: raise RuntimeError(f'empty execution handler result for {stage}')
                self.store.checkpoint(self.experiment_id,stage,updates,lease_token=token,status='succeeded')
            return cleanup('COMPLETED','completed')
        except Exception as exc:
            try: self.store.checkpoint(self.experiment_id,stage,{'stage_error':f'{type(exc).__name__}: {exc}'},lease_token=token,status='failed')
            except RuntimeError: raise  # fenced workers cannot clean resources owned by a newer lease
            cancelled=self.store.get(self.experiment_id)['state']=='CANCEL_REQUESTED'
            return cleanup('CANCELLED' if cancelled else 'FAILED','cancelled' if cancelled else 'failed',{'error':f'{type(exc).__name__}: {exc}'})
        finally:
            stop.set(); thread.join(timeout=1); self.store.release(self.experiment_id,token)
