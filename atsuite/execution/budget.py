from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Budget:
    max_candidates: int | None = None
    max_builds: int | None = None
    max_trials: int | None = None
    max_wall_time_seconds: int | None = None
    max_estimated_spend: float | None = None
    reserved_spend: float = 0.0
    candidates: int = 0
    builds: int = 0
    trials: int = 0

    def reserve(self, *, candidates: int = 0, builds: int = 0, trials: int = 0, spend: float = 0.0) -> None:
        if self.max_candidates is not None and self.candidates + candidates > self.max_candidates:
            raise RuntimeError("candidate budget exceeded")
        if self.max_builds is not None and self.builds + builds > self.max_builds:
            raise RuntimeError("build budget exceeded")
        if self.max_trials is not None and self.trials + trials > self.max_trials:
            raise RuntimeError("trial budget exceeded")
        if self.max_estimated_spend is not None and self.reserved_spend + spend > self.max_estimated_spend:
            raise RuntimeError("estimated spend budget exceeded")
        self.candidates += candidates
        self.builds += builds
        self.trials += trials
        self.reserved_spend += spend

class BudgetController:
    """Durable Decimal reservations. Estimates are limits on dispatch, not a billing guarantee."""
    def __init__(self,store,experiment_id,*,lease_token=None):
        self.store=store; self.experiment_id=experiment_id; self.lease_token=lease_token

    def reserve(self,*,reservation_id=None,spend=0,llm_tokens=0,**counts):
        import json,time,secrets
        from decimal import Decimal
        from datetime import datetime,timezone
        reservation_id=reservation_id or 'reservation-'+secrets.token_hex(12)
        amount=Decimal(str(spend))
        if amount < 0 or not amount.is_finite() or isinstance(llm_tokens,bool) or not isinstance(llm_tokens,int) or llm_tokens<0:
            raise ValueError('invalid budget reservation')
        with self.store._transaction():
            self.store._check_lease(self.experiment_id,self.lease_token)
            record=self.store.get(self.experiment_id); policy=record['plan']['spec']['budget']
            started=record.get('stage_records',{}).get('PREFLIGHT',{}).get('started_at')
            began=datetime.fromisoformat(started).timestamp() if started else time.time()
            ledger=record.setdefault('budget_ledger',{'started_at':began,'reservations':{},'currency':policy['currency']})
            previous=ledger['reservations'].get(reservation_id)
            value={'amount':str(amount),'llm_tokens':llm_tokens,'status':'reserved','counts':counts}
            if previous:
                if previous['amount']!=value['amount'] or previous['llm_tokens']!=llm_tokens or previous['counts']!=counts: raise ValueError('reservation identity reused')
                return reservation_id
            spec=record['plan']['spec']
            candidate_limit=spec.get('search',{}).get('max_candidates')
            protocol=spec.get('protocol',{})
            trial_limit=candidate_limit*sum(protocol.get(k,0) for k in ('warmup_runs','exploration_runs','confirmation_runs')) if candidate_limit is not None else None
            for name,limit in (('candidates',candidate_limit),('builds',candidate_limit),('trials',trial_limit)):
                count=counts.get(name,0)
                if isinstance(count,bool) or not isinstance(count,int) or count<0:raise ValueError('invalid count reservation')
                used=sum(r.get('counts',{}).get(name,0) for r in ledger['reservations'].values() if r['status']!='released')
                if limit is not None and used+count>limit:raise RuntimeError(name.rstrip('s')+' budget exceeded')
            wall=policy.get('max_wall_time_seconds')
            if wall is not None and time.time()-ledger['started_at']>wall and counts.get('cleanup')!=True:
                raise RuntimeError('wall time budget exceeded')
            reserved=sum(max(Decimal(r['amount']),Decimal(r.get('observed_spend') or 0)) for r in ledger['reservations'].values() if r['status']!='released')
            cap=policy.get('max_estimated_spend')
            reserve=Decimal(str(cap or 0))*Decimal('.1') if policy.get('include_cleanup_reserve') else Decimal(0)
            ceiling=Decimal(str(cap))-(0 if counts.get('cleanup') else reserve) if cap is not None else None
            if ceiling is not None and reserved+amount>ceiling: raise RuntimeError('estimated spend budget exceeded')
            tokens=sum(r['llm_tokens'] for r in ledger['reservations'].values() if r['status']!='released')
            if policy.get('max_llm_tokens') is not None and tokens+llm_tokens>policy['max_llm_tokens']: raise RuntimeError('LLM token budget exceeded')
            ledger['reservations'][reservation_id]=value
            self.store.connection.execute('UPDATE experiments SET payload=? WHERE id=?',(json.dumps(record,sort_keys=True),self.experiment_id))
            self.store._append_event(self.experiment_id,{'event':'budget_reserved','reservation_id':reservation_id,**value})
        return reservation_id

    def settle(self,reservation_id,*,observed_spend=None,pending=False):
        import json
        from decimal import Decimal
        if observed_spend is not None and (not Decimal(str(observed_spend)).is_finite() or Decimal(str(observed_spend))<0):raise ValueError('invalid observed spend')
        with self.store._transaction():
            self.store._check_lease(self.experiment_id,self.lease_token)
            record=self.store.get(self.experiment_id)
            entry=record['budget_ledger']['reservations'][reservation_id]
            if entry['status']=='settled': return
            entry['status']='pending' if pending else 'settled'
            entry['observed_spend']=str(observed_spend) if observed_spend is not None else None
            self.store.connection.execute('UPDATE experiments SET payload=? WHERE id=?',(json.dumps(record,sort_keys=True),self.experiment_id))
            self.store._append_event(self.experiment_id,{'event':'budget_settled','reservation_id':reservation_id,**entry})
