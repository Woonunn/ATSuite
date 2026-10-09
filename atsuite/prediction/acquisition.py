"""Bounded search selection. Predictions never become confirmation evidence."""
from __future__ import annotations
from dataclasses import dataclass
import math
from atsuite.prediction.features import candidate_value, FEATURE_VERSION

def _pilot_features(pilot,candidate,candidates):
    if not pilot:return None
    if any(isinstance(value,(int,float)) and not isinstance(value,bool) for value in pilot.values()):return pilot
    lookup={candidate_value(c,'candidate_id',''):c for c in candidates}
    samples=[row for cid,row in pilot.items() if isinstance(row,dict) and
        candidate_value(lookup.get(cid,{}),'provider','')==candidate_value(candidate,'provider','') and
        candidate_value(lookup.get(cid,{}),'family','')==candidate_value(candidate,'family','')]
    features={}
    for name in ('latency_ms','failure_rate','cost'):
        values=[row[name] for row in samples if isinstance(row.get(name),(int,float)) and not isinstance(row[name],bool) and math.isfinite(row[name])]
        if values:features[name]=sum(values)/len(values)
    if samples:features['observed_candidates']=len(samples)
    return features or None

@dataclass(frozen=True)
class Acquisition:
    candidates: tuple
    audit: tuple[dict, ...]
    mode: str

def select_next_candidates(workload, candidates, *, predictor=None, objective='min_latency', limit=1, pilot=None, observed_ids=(), model_quality=None, exploration_ratio=.2):
    if isinstance(limit,bool) or not isinstance(limit,int) or limit < 0:
        raise ValueError('limit must be nonnegative integer')
    if not 0 <= exploration_ratio <= 1:
        raise ValueError('exploration_ratio outside [0,1]')
    objective_type = candidate_value(objective,'type',objective)
    pending = [c for c in candidates if candidate_value(c,'candidate_id',candidate_value(c,'id','')) not in set(observed_ids)]
    model = getattr(predictor,'model',None)
    quality = model_quality or getattr(model,'quality','insufficient_real_data')
    predictions, fallback = {}, 'ml_disabled' if predictor is None else ''
    pilot_inputs={}
    if predictor is not None and quality not in {'evaluated_real_holdout','approved_local_contract'}:
        fallback = 'model_quality_insufficient'
    if predictor is not None and not fallback:
        for index,candidate in enumerate(pending):
            try:
                pilot_inputs[index]=_pilot_features(pilot,candidate,candidates)
                prediction = predictor.predict(workload,candidate,pilot=pilot_inputs[index])
                target = 'cost' if 'cost' in str(objective_type) else 'latency_ms'
                value = prediction.values.get(target)
                if prediction.ood or prediction.quality != 'predicted' or prediction.feature_version != FEATURE_VERSION or value is None or not math.isfinite(value):
                    fallback = prediction.reason or 'prediction_unsupported'
                    break
                predictions[index] = prediction
            except Exception as exc:
                fallback = 'predictor_error:' + type(exc).__name__
                break
    indices = list(range(len(pending)))
    if not fallback:
        target = 'cost' if 'cost' in str(objective_type) else 'latency_ms'
        indices.sort(key=lambda i: (predictions[i].values[target],i))
    # Ensure each provider/family has at least one pilot before optimized picks.
    covered = {(candidate_value(c,'provider',''),candidate_value(c,'family','')) for c in candidates if candidate_value(c,'candidate_id',candidate_value(c,'id','')) in set(observed_ids)}
    selected, audit = [], []
    for index in range(len(pending)):
        candidate = pending[index]
        group = (candidate_value(candidate,'provider',''),candidate_value(candidate,'family',''))
        if group not in covered and len(selected) < limit:
            selected.append(index); covered.add(group)
            audit.append({'candidate_id':candidate_value(candidate,'candidate_id',candidate_value(candidate,'id','')),'reason':'provider_family_coverage'})
    exploration_slots = math.floor(limit * exploration_ratio) if predictor is not None and not fallback else 0
    if limit == 1 and exploration_ratio and len(observed_ids) % max(1,round(1 / exploration_ratio)) == 0 and predictor is not None and not fallback:
        exploration_slots = 1
    for index in list(range(len(pending)))[:exploration_slots] + indices:
        if len(selected) >= limit:
            break
        if index in selected:
            continue
        selected.append(index)
        prediction = predictions.get(index)
        audit.append({'candidate_id':candidate_value(pending[index],'candidate_id',candidate_value(pending[index],'id','')), 'reason':fallback or ('exploration_reserve' if index < exploration_slots else 'predicted_acquisition'), 'model_id':getattr(prediction,'model_id',''), 'feature_version':FEATURE_VERSION, 'prediction':getattr(prediction,'values',{}), 'model_quality':quality,'training_data_digest':getattr(model,'training_data_digest',''),'pilot':pilot_inputs.get(index)})
    return Acquisition(tuple(pending[index] for index in selected),tuple(audit),'grid_fallback' if fallback else 'model_guided')
