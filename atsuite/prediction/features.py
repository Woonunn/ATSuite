from __future__ import annotations
import hashlib
import json
import math
from typing import Any, Mapping

FEATURE_VERSION = '2'
PROVIDERS = ('aws_lambda', 'aws_agentcore', 'ali_fc', 'ali_agentrun', 'gcp_faas', 'gcp_mcp', 'mcp_gateway')
REGIONS = ('us-east-1','us-west-2','eu-west-1','cn-hangzhou','cn-shanghai','us-central1','asia-east1','europe-west1')
MODES = ('on_demand','ondemand','request_based','instance_based','pay_as_you_go','session','faas')

def candidate_value(candidate, field, default=None):
    return candidate.get(field, default) if isinstance(candidate, Mapping) else getattr(candidate, field, default)

def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def _categorical(result, name, value, vocabulary):
    value = str(value or '')
    for category in (*vocabulary, 'unknown'):
        result[f'{name}.{category}'] = float(value == category if category != 'unknown' else value not in vocabulary)

def workload_features(workload: Mapping[str, Any]) -> dict[str, float]:
    result = {key: float(workload[key]) for key in ('total_nodes','tool_use_count','dag_depth','max_parallelism','tool_call_density','stateful_tool_ratio','stateful_invocation_ratio','rw_ratio') if _number(workload.get(key))}
    for key in ('scenario_id','state_semantics','workload_family'):
        if key in workload:
            _hash_categories(result, key, workload[key])
    return result

def _hash_categories(result, name, value):
    # Stable bounded encoding; vocabulary/hash version is frozen with feature version.
    canonical = json.dumps(value, sort_keys=True, default=str, separators=(',',':'))
    bucket = int(hashlib.sha256(canonical.encode()).hexdigest()[:8],16) % 32
    for index in range(32):
        result[f'{name}.bucket{index}'] = float(index == bucket)

def candidate_features(candidate: Any) -> dict[str, float]:
    resources = candidate_value(candidate, 'resources_by_target', {}) or {}
    values = [resource for resource in resources.values() if isinstance(resource, Mapping)]
    result = {'target_count':float(len(values)), 'family_session':float(candidate_value(candidate,'family','') == 'session'), 'arch_arm64':float('arm64' in str(candidate_value(candidate,'runtime_architecture','')))}
    for resource in ('cpu','memory_mb','disk_mb','concurrency'):
        numeric = [float(v[resource]) for v in values if _number(v.get(resource))]
        result[f'total_{resource}'] = sum(numeric)
        result[f'min_{resource}'] = min(numeric) if numeric else 0.0
        result[f'max_{resource}'] = max(numeric) if numeric else 0.0
        result[f'missing_{resource}'] = float(len(values) - len(numeric))
    _hash_categories(result,'resource_distribution',resources)
    _categorical(result,'provider',candidate_value(candidate,'provider',''),PROVIDERS)
    _categorical(result,'region',candidate_value(candidate,'region',''),REGIONS)
    _categorical(result,'mode',candidate_value(candidate,'billing_mode',candidate_value(candidate,'family','')),MODES)
    for field in ('service','logical_topology','state_semantics','scenario_id','runtime_architecture'):
        _hash_categories(result,field,candidate_value(candidate,field,''))
    return result

def make_features(workload: Mapping[str,Any], candidate: Any, *, pilot=None):
    if not isinstance(workload,Mapping):
        workload=vars(workload)
    result = {f'workload.{key}': value for key,value in workload_features(workload).items()}
    result.update({f'candidate.{key}':value for key,value in candidate_features(candidate).items()})
    if pilot:
        result.update({f'pilot.{key}':float(value) for key,value in pilot.items() if _number(value)})
    return result
