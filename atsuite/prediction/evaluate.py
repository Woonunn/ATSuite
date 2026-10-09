from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from atsuite.prediction.dataset import Dataset
from atsuite.prediction.predictor import Predictor


def evaluate(predictor: Predictor,dataset:Dataset,*,top_k=1):
    if isinstance(top_k,bool) or not isinstance(top_k,int) or top_k<1:
        raise ValueError('top_k must be positive integer')
    errors=defaultdict(list); groups=defaultdict(list); predictions=0;ood=0
    model=predictor.model
    for row in dataset.rows:
        if model is None:continue
        predictions+=1
        outside=False
        for name,(lower,upper) in model.feature_bounds.items():
            value=row.features.get(name)
            if value is None or not lower-max(abs(upper-lower)*.2,1e-9)<=value<=upper+max(abs(upper-lower)*.2,1e-9):outside=True
        ood+=outside
        predicted={}
        for target,coefficients in model.coefficients.items():
            value=max(0.0,coefficients.get('intercept',0.0)+sum(coefficients.get(name,0.0)*row.features.get(name,0.0)/model.scales.get(name,1.0) for name in model.feature_names))
            if target in {'failure_risk','failure_rate'}:value=min(1.0,value)
            predicted[target]=value
            if target in row.targets:errors[target].append(abs(value-row.targets[target]))
        if 'latency_ms' in row.targets and 'latency_ms' in predicted:
            groups[(row.workload_fingerprint,row.scenario_id)].append((row.candidate_id,predicted['latency_ms'],row.targets['latency_ms']))
    per_target={name:{'samples':len(values),'mae':sum(values)/len(values)} for name,values in sorted(errors.items()) if values}
    all_errors=[v for values in errors.values() for v in values]
    recalls=[];regrets=[]
    for rows in groups.values():
        if len(rows)<2:continue
        predicted=sorted(rows,key=lambda r:(r[1],r[0]))[:top_k]
        actual=sorted(rows,key=lambda r:(r[2],r[0]))[:top_k]
        recalls.append(len({r[0] for r in predicted}&{r[0] for r in actual})/len(actual))
        regrets.append(min(r[2] for r in predicted)-min(r[2] for r in rows))
    real=bool(dataset.rows) and all(r.measurement_source=='platform_measured' and r.source_ref and r.trial_ids for r in dataset.rows)
    families={r.workload_family or r.workload_fingerprint for r in dataset.rows}
    trials={t for r in dataset.rows for t in r.trial_ids}
    independent=bool(model and model.training_data_digest) and not families.intersection(model.training_workload_families) and not trials.intersection(model.training_trial_ids) and all(r.observed_at>model.trained_through for r in dataset.rows)
    spend=sum((Decimal(r.observed_spend) for r in dataset.rows),Decimal('0')) if dataset.rows and all(r.observed_spend is not None for r in dataset.rows) else None
    return {'rows':len(dataset.rows),'predictions':predictions,'per_target':per_target,'mae':sum(all_errors)/len(all_errors) if all_errors else None,
            'status':'evaluated' if all_errors else 'insufficient_evidence','model_quality':'evaluated_real_holdout' if real and independent and all_errors else 'insufficient_real_data',
            'holdout_independent':independent,'top_k':top_k,'acquisition_recall':sum(recalls)/len(recalls) if recalls else None,
            'reference_search_regret':sum(regrets)/len(regrets) if regrets else None,'actual_trial_calls':len(trials) if trials else None,
            'actual_trial_spend':str(spend) if spend is not None else None,'ood_rows':ood,'ood_coverage':ood/predictions if predictions else None}
