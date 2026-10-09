from __future__ import annotations

import copy
import json
import shutil
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from atsuite.pipeline import PROVIDERS, resolve_benchmark
from atsuite.recommender.capabilities import describe_capability, validate_candidate
from atsuite.recommender.contracts import CandidateSpec, ExperimentSpec, utc_now
from atsuite.recommender.identity import candidate_id, digest, file_digest
from atsuite.recommender.resources import local_resource_candidates
from atsuite.recommender.topology import TopologyConstraints, TopologyPlan, compile_session_config, generate_topologies, generate_topology, nodes_from_config, normalize_config
from atsuite.utils import resolve_benchmark_root


def _canonical_groups(groups):
    return tuple(sorted(tuple(sorted(group)) for group in groups))


def _bundle_manifest(root: Path):
    rows = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(part in {'__pycache__', '.DS_Store', '.git'} for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f'benchmark bundle symlink is not permitted: {relative}')
        if path.is_file():
            rows.append({'path': relative.as_posix(), 'sha256': file_digest(path), 'mode': path.stat().st_mode & 0o777})
    return rows


def _requested_targets(config, resolved, topology=None):
    family = resolved.family
    payload = config['pipeline'][family]
    raw_targets = payload['units' if family == 'faas' else 'servers']
    if topology is not None and _canonical_groups(topology.groups) != _canonical_groups(target.node_names for target in resolved.targets.values()):
        raw_targets = []
        for index, group in enumerate(topology.groups):
            existing = next((target for target in payload['servers'] if set(target['nodes']) == set(group)), None)
            target = copy.deepcopy(existing or payload['servers'][0])
            if not existing:
                target['name'] = f'server_{index}'
            target['nodes'] = list(group)
            raw_targets.append(target)
    requested = {target['name']: copy.deepcopy(target) for target in raw_targets}
    return requested


def _resources(requested, resolved, capability):
    resources = {}
    for name, target in requested.items():
        raw = target['deploy']
        original = resolved.targets.get(name)
        # A generated topology inherits its template's actual requested config,
        # while the baseline keeps each target's distinct runtime unchanged.
        runtime = original.runtime if original else next(iter(resolved.targets.values())).runtime
        resources[name] = {'cpu': raw.get('cpu', runtime.cpu), 'memory_mb': raw.get('memory', runtime.memory),
            'disk_mb': raw.get('disk', runtime.disk), 'timeout_s': raw.get('timeout', runtime.timeout),
            'architecture': raw.get('architecture', capability.architectures[0]),
            'extras': {key: value for key, value in raw.items() if key not in {'cpu', 'memory', 'disk', 'timeout', 'architecture'}}}
    return resources


def _candidate_from_resolved(spec, config_path, provider, region='', topology=None, resource_variant=0, *, resources=None, workload=None):
    resolved = resolve_benchmark(config_path, provider)
    config = normalize_config(json.loads(config_path.read_text()), config_path)
    capability = describe_capability(provider, region)
    requested = _requested_targets(config, resolved, topology)
    baseline = _resources(requested, resolved, capability)
    if resources is None:
        variants = list(local_resource_candidates(baseline, provider, region=region, deployment_mode=capability.deployment_mode))
        resources = variants[min(resource_variant, len(variants)-1)][0]
    groups = topology.groups if topology else tuple(tuple(target.node_names) for target in resolved.targets.values())
    workload = workload or digest({'bundle': _bundle_manifest(resolved.bench_root)}, length=32)
    architecture = next(iter(resources.values()))['architecture']
    identity = dict(workload_fingerprint=workload, provider=provider, family=resolved.family,
        deployment_mode=capability.deployment_mode, region=region or 'default', topology={'groups': groups},
        resources_by_target=resources, runtime_architecture=architecture, state_policy='isolated_uid', lifecycle_policy='ephemeral')
    return CandidateSpec(candidate_id(**identity), workload, provider, resolved.family, capability.deployment_mode,
        region or 'default', architecture, groups, resources, 'isolated_uid', 'ephemeral', capability.snapshot_id, file_digest(config_path))


def plan_experiment(spec: ExperimentSpec) -> dict[str, Any]:
    config_path = Path(spec.benchmark_config).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(config_path)
    config = normalize_config(json.loads(config_path.read_text()), config_path)
    root = resolve_benchmark_root(config_path)
    manifest = _bundle_manifest(root)
    bundle_digest = digest(manifest, length=64)
    workload = digest({'bundle': bundle_digest, 'config': config_path.relative_to(root).as_posix()}, length=32)
    capabilities, exclusions, baselines, all_topologies = {}, [], {}, {}
    provider_candidates = {}
    for provider in spec.providers:
        baselines[provider] = {'family': PROVIDERS[provider].family, 'source_config': copy.deepcopy(config),
                               'valid': False, 'blocking_reasons': ['provider_resolution_pending']}
        try:
            resolved = resolve_benchmark(config_path, provider)
            region = spec.regions_by_provider.get(provider, 'default')
            capability = describe_capability(provider, region)
            capabilities[provider] = asdict(capability)
            nodes, baseline = nodes_from_config(config_path, provider)
            options = TopologyConstraints(max_candidates=spec.search.max_generated_topologies)
            baseline_plan = generate_topology(nodes, 'baseline', baseline_groups=baseline, constraints=options)
            if resolved.family == 'session':
                for strategy in spec.search.topology_strategies:
                    trial = generate_topology(nodes, strategy, baseline_groups=baseline, constraints=options)
                    for reason in trial.blocking_reasons:
                        exclusions.append({'provider': provider, 'strategy': strategy, 'reason_code': reason.split(':')[0], 'reason': reason})
            topologies = generate_topologies(nodes, strategies=('baseline', *spec.search.topology_strategies), baseline_groups=baseline, constraints=options) if resolved.family == 'session' else [baseline_plan]
            baselines[provider] = {'family': resolved.family, 'source_config': copy.deepcopy(config),
                'groups': baseline, 'valid': baseline_plan.valid, 'blocking_reasons': list(baseline_plan.blocking_reasons)}
            if not baseline_plan.valid:
                exclusions.append({'provider': provider, 'reason_code': 'invalid_baseline_topology',
                                   'reasons': list(baseline_plan.blocking_reasons)})
            rows, seen = [], set()
            for topology in topologies:
                if not topology.valid:
                    exclusions.append({'provider': provider, 'reason_code': 'invalid_topology', 'reasons': list(topology.blocking_reasons)})
                    continue
                all_topologies[topology.topology_id] = topology.to_dict()
                requested = _requested_targets(config, resolved, topology)
                baseline_resources = _resources(requested, resolved, capability)
                for resource_values, audit in local_resource_candidates(baseline_resources, provider, region=region, deployment_mode=capability.deployment_mode):
                    candidate = _candidate_from_resolved(spec, config_path, provider, region, topology, resources=resource_values, workload=workload)
                    result = validate_candidate(candidate)
                    if audit['source'] == 'exact_baseline' and _canonical_groups(topology.groups) == _canonical_groups(baseline):
                        baselines[provider]['resources_by_target'] = copy.deepcopy(resource_values)
                        baselines[provider]['resource_validation'] = asdict(result)
                    if not result.valid:
                        exclusions.append({'provider': provider, 'candidate_id': candidate.candidate_id,
                            'reason_code': 'invalid_baseline_resources' if audit['source'] == 'exact_baseline' else 'invalid_resources',
                            'reasons': list(result.reasons), 'requested_resources': resource_values})
                        continue
                    if candidate.candidate_id in seen:
                        continue
                    seen.add(candidate.candidate_id)
                    row = asdict(candidate)
                    row.update(resource_source=audit['source'], search_audit=audit, requested_target_configs=requested,
                        logical_to_physical={node: name for name, target in requested.items()
                                             for node in target.get('nodes', [target.get('node')]) if node},
                        resource_semantics=capability.resource_semantics, planning_warnings=list(result.warnings),
                        topology_id=topology.topology_id, topology_strategies=topology.strategies,
                        baseline=_canonical_groups(topology.groups) == _canonical_groups(baseline) and audit['source'] == 'exact_baseline', bundle_digest=bundle_digest)
                    rows.append(row)
            provider_candidates[provider] = deque(rows)
            if not rows:
                exclusions.append({'provider': provider, 'reason_code': 'no_legal_candidates'})
        except (ValueError, SystemExit) as exc:
            baselines[provider]['blocking_reasons'] = [str(exc)]
            exclusions.append({'provider': provider, 'reason_code': 'unsupported_configuration', 'reason': str(exc)})
    if not capabilities:
        raise ValueError('none of the requested providers can resolve this benchmark')
    candidates = []
    eligible_provider_count = sum(bool(rows) for rows in provider_candidates.values())
    reserve = 1 if spec.search.mode == 'adaptive' and spec.advisor_enabled and spec.search.max_candidates > eligible_provider_count else 0
    static_limit = spec.search.max_candidates - reserve
    # One provider/family point per round precedes any extra allocation.
    while len(candidates) < static_limit and any(provider_candidates.values()):
        for provider in spec.providers:
            if len(candidates) >= static_limit:
                break
            if provider_candidates.get(provider):
                candidates.append(provider_candidates[provider].popleft())
    selected = {row['provider'] for row in candidates}
    skipped = [provider for provider, rows in provider_candidates.items() if provider not in selected and rows]
    for provider in skipped:
        exclusions.append({'provider': provider, 'reason_code': 'skipped_by_budget'})
    withheld = []
    if reserve:
        omitted = next((provider_candidates[provider][0] for provider in spec.providers if provider_candidates.get(provider)), None)
        if omitted:
            withheld = [{key: copy.deepcopy(omitted[key]) for key in
                ('provider', 'region', 'family', 'deployment_mode', 'logical_topology', 'resources_by_target', 'runtime_architecture')}]
    return {'schema_version': 1, 'status': 'planned', 'created_at': utc_now(), 'spec': spec.to_dict(),
        'source_bundle_root': str(root), 'bundle_digest': bundle_digest, 'bundle_manifest': manifest,
        'bundle_directories': [p.relative_to(root).as_posix() for p in sorted(root.rglob('*')) if p.is_dir() and not p.is_symlink()
                               and not any(part in {'__pycache__', '.git'} for part in p.relative_to(root).parts)],
        'scope': {'workload_fingerprint': workload, 'search_exhaustive': False, 'search_seed': 0,
                  'generated_candidates': len(candidates), 'measured_candidates': 0, 'confirmed_candidates': 0,
                  'reserved_candidate_slots': reserve,
                  'requested_providers': list(spec.providers), 'planned_providers': sorted(selected),
                  'coverage_complete': set(spec.providers) <= selected, 'coverage_insufficient': bool(skipped),
                  'skipped_by_budget': skipped},
        'candidates': candidates, 'withheld_candidate_proposals': withheld, 'capabilities': capabilities, 'excluded': exclusions, 'baselines': baselines,
        'topologies': list(all_topologies.values()), 'promising_unverified': [], 'best_verified': None, 'advisor_alternative': None}


def _apply_candidate_resources(config, candidate):
    family = candidate['family']
    targets = config['pipeline'][family]['units' if family == 'faas' else 'servers']
    for target in targets:
        raw = candidate['resources_by_target'].get(target['name'])
        if raw is None:
            raise ValueError(f'missing resources for target {target["name"]}')
        target.setdefault('deploy', {})
        target['deploy'].update({'cpu': raw['cpu'], 'memory': raw['memory_mb'], 'disk': raw['disk_mb'], 'timeout': raw['timeout_s'],
            **raw.get('extras', {}), 'region': candidate['region'], 'architecture': raw['architecture']})


def _bundle_references(config, source_root, bundle_root):
    # Resolve absolute references into the frozen bundle, retaining relative
    # references for Docker build contexts and benchmark data lookup.
    def relocate(raw):
        path = Path(str(raw))
        absolute = path.resolve() if path.is_absolute() else (source_root / path).resolve()
        try:
            relative = absolute.relative_to(source_root)
        except ValueError:
            raise ValueError(f'benchmark reference escapes bundle: {raw}') from None
        return './' + relative.as_posix()
    config['trace_file'] = relocate(config['trace_file'])
    for node in config.get('nodes', []):
        if node.get('dir'):
            node['dir'] = relocate(node['dir'])
    reference_keys = {'path', 'dir', 'trace_file', 'data_file', 'snapshot_file', 'oracle_file', 'init_script',
                      'requirements_file', 'ledger_path', 'fx_path', 'file', 'oracle', 'state_snapshot'}
    def walk(value):
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if key in reference_keys and isinstance(item, str) and item:
                    value[key] = relocate(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(config)
    return config


def materialize_plan(plan, output):
    path = Path(output).resolve()
    if path.exists():
        raise ValueError(f'plan artifact is immutable and already exists: {path}')
    source = Path(plan['spec']['benchmark']['config']).expanduser().resolve()
    source_root = Path(plan.get('source_bundle_root', resolve_benchmark_root(source)))
    manifest = plan.get('bundle_manifest') or _bundle_manifest(source_root)
    if digest(manifest, length=64) != plan.get('bundle_digest', digest(manifest, length=64)):
        raise ValueError('bundle manifest digest mismatch')
    for entry in manifest:
        original = source_root / entry['path']
        if original.is_symlink() or not original.is_file() or file_digest(original) != entry['sha256']:
            raise ValueError(f'benchmark changed after planning: {entry["path"]}')
    source_config = normalize_config(json.loads(source.read_text()), source)
    topology_by_id = {row['topology_id']: row for row in plan.get('topologies', [])}
    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in plan.get('candidates', []):
        bundle_root = path.parent / 'candidates' / candidate['candidate_id'] / 'bundle'
        if bundle_root.exists():
            raise ValueError('candidate bundle is immutable and already exists')
        for directory in plan.get('bundle_directories', []):
            (bundle_root / directory).mkdir(parents=True, exist_ok=True)
        for entry in manifest:
            original = source_root / entry['path']
            destination = bundle_root / entry['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
        frozen_source = bundle_root / source.relative_to(source_root)
        if not frozen_source.exists():
            frozen_source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, frozen_source)
        normalized = _bundle_references(normalize_config(json.loads(source.read_text()), source),
            Path(plan.get('reference_root', source_root)), bundle_root)
        normalized_source = bundle_root / 'config' / 'recommender_source.json'
        normalized_source.parent.mkdir(parents=True, exist_ok=True)
        normalized_source.write_text(json.dumps(normalized, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
        if candidate['family'] == 'session':
            row = topology_by_id.get(candidate['topology_id'])
            if row is None:
                raise ValueError('missing candidate topology')
            topology = TopologyPlan(**{**row, 'groups': tuple(tuple(group) for group in row['groups'])})
            config = compile_session_config(normalized_source, topology)
        else:
            config = copy.deepcopy(normalized)
        _apply_candidate_resources(config, candidate)
        generated = bundle_root / 'config' / 'generated_config.json'
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(json.dumps(config, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
        candidate.update(generated_config=str(generated), source_config=str(frozen_source),
            normalized_source_config=str(normalized_source), normalized_source_digest=file_digest(normalized_source),
            original_source_config=str(source), generated_config_digest=file_digest(generated),
            bundle_root=str(bundle_root), bundle_digest=plan.get('bundle_digest'), bundle_manifest=copy.deepcopy(manifest))
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False, allow_nan=False, default=str) + '\n')
    return path


def write_plan(spec, output):
    return materialize_plan(plan_experiment(spec), output)


def materialize_child_candidate(parent_plan: dict[str, Any], candidate_proposal_spec: dict[str, Any], output: str | Path) -> dict[str, Any]:
    """Validate and freeze one proposal from the parent's existing bundle.

    Approval, search maxima, and budget remain service responsibilities. This
    adapter permits no execution/permission changes and never edits its parent.
    """
    from dataclasses import replace
    from atsuite.recommender.topology import validate_topology

    proposal = copy.deepcopy(candidate_proposal_spec)
    allowed = {'provider', 'region', 'family', 'logical_topology', 'resources_by_target', 'runtime_architecture',
               'deployment_mode', 'strategy', 'state_policy', 'lifecycle_policy'}
    if set(proposal) - allowed:
        raise ValueError('proposal contains unsupported authority or configuration fields: ' + ', '.join(sorted(set(proposal)-allowed)))
    provider = proposal.get('provider')
    if provider not in parent_plan['spec']['providers']:
        raise ValueError('proposal provider is outside the parent plan')
    parent = next((candidate for candidate in parent_plan.get('candidates', []) if candidate['provider'] == provider and candidate.get('baseline')), None)
    parent = parent or next((candidate for candidate in parent_plan.get('candidates', []) if candidate['provider'] == provider), None)
    if parent is None or not parent.get('bundle_root') or not parent.get('normalized_source_config'):
        raise ValueError('proposal requires a materialized frozen parent candidate')
    region = proposal.get('region') or parent['region']
    if region != parent['region']:
        raise ValueError('proposal region is outside the frozen parent plan')
    family = proposal.get('family') or parent['family']
    if family != parent['family']:
        raise ValueError('proposal family does not match the provider adapter')
    for field in ('state_policy', 'lifecycle_policy', 'deployment_mode'):
        if proposal.get(field) not in (None, parent[field]):
            raise ValueError(f'proposal cannot change frozen {field}')
    source = Path(parent['normalized_source_config'])
    if not source.is_file() or file_digest(source) != parent['normalized_source_digest']:
        raise ValueError('frozen parent source configuration digest mismatch')
    nodes, baseline = nodes_from_config(source, provider)
    groups = _canonical_groups(proposal.get('logical_topology') or parent['logical_topology'])
    if family == 'faas' and groups != _canonical_groups(baseline):
        raise ValueError('FaaS proposal cannot regroup single-node units')
    topology = generate_topology(nodes, 'baseline', baseline_groups=groups)
    problems = validate_topology(topology, (node.name for node in nodes))
    if problems:
        raise ValueError('invalid proposal topology: ' + '; '.join(problems))
    topology = replace(topology, strategy='proposal', strategies=('proposal',))
    if family == 'session':
        # Repeat the ordinary compiler's state-atom and config checks before
        # publishing any child artifact.
        compiled = compile_session_config(source, topology)
    else:
        compiled = normalize_config(json.loads(source.read_text()), source)
    resolved = resolve_benchmark(source, provider)
    requested = _requested_targets(normalize_config(json.loads(source.read_text()), source), resolved, topology)
    capability = describe_capability(provider, region, parent['deployment_mode'])
    inherited = _resources(requested, resolved, capability)
    proposal_resources = proposal.get('resources_by_target')
    if proposal_resources is not None:
        if set(proposal_resources) != set(inherited):
            raise ValueError('proposal resources must cover every physical target exactly')
        for target, fields in proposal_resources.items():
            if not isinstance(fields, dict) or set(fields) - {'cpu', 'memory_mb', 'disk_mb', 'timeout_s', 'architecture', 'extras'}:
                raise ValueError('proposal resources contain unsupported fields')
            original_extras = inherited[target].get('extras', {})
            if fields.get('extras'):
                changed_extras = {key for key, value in fields['extras'].items() if value != original_extras.get(key)}
                if not changed_extras <= {'concurrency', 'billing_mode', 'execution_environment'} or not changed_extras <= set(capability.tunable_fields):
                    raise ValueError('proposal cannot change lifecycle, environment, or permissions')
                fields['extras'] = {**original_extras, **fields['extras']}
            inherited[target].update(copy.deepcopy(fields))
    architecture = proposal.get('runtime_architecture') or parent['runtime_architecture']
    for raw in inherited.values():
        raw['architecture'] = architecture
    spec = ExperimentSpec.from_dict({**copy.deepcopy(parent_plan['spec']), 'benchmark': {
        **parent_plan['spec']['benchmark'], 'config': str(source)}})
    candidate = _candidate_from_resolved(spec, source, provider, region, topology, resources=inherited,
        workload=parent['workload_fingerprint'])
    validation = validate_candidate(candidate)
    if not validation.valid:
        raise ValueError('invalid proposal resources: ' + '; '.join(validation.reasons))
    row = asdict(candidate)
    row.update(resource_source='validated_proposal', search_audit={'source': 'advisor_or_acquisition_proposal'},
        requested_target_configs=requested, topology_id=topology.topology_id, topology_strategies=topology.strategies,
        logical_to_physical={node: target for target, value in requested.items() for node in value.get('nodes', [value.get('node')]) if node},
        resource_semantics=capability.resource_semantics, planning_warnings=list(validation.warnings), baseline=False,
        bundle_digest=parent_plan['bundle_digest'], parent_plan_digest=digest(parent_plan, length=64),
        proposal_digest=digest(proposal, length=64))
    child = copy.deepcopy(parent_plan)
    child.update(parent_plan_digest=digest(parent_plan, length=64), proposal_digest=row['proposal_digest'],
        source_bundle_root=parent['bundle_root'], reference_root=parent['bundle_root'],
        bundle_manifest=copy.deepcopy(parent['bundle_manifest']),
        bundle_directories=[path.relative_to(Path(parent['bundle_root'])).as_posix()
            for path in Path(parent['bundle_root']).rglob('*') if path.is_dir()],
        spec=spec.to_dict(), candidates=[row], topologies=[topology.to_dict()],
        created_at=utc_now(), status='planned_child')
    materialize_plan(child, output)
    return child['candidates'][0]
