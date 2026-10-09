from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from atsuite.pipeline import _nodes_from_tools, logical_state_domain, resolve_benchmark
from atsuite.utils import resolve_benchmark_root
from atsuite.recommender.identity import digest, file_digest


@dataclass(frozen=True)
class TopologyNode:
    name: str
    state_domain: str = ''
    state_backend: str = ''
    shareable_state: bool = False
    must_colocate: tuple[str, ...] = ()
    must_separate: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    python_version: str = ''
    system_dependencies: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    isolation_class: str = 'default'
    parallel_safe: bool = True
    preferred_resource_class: str = 'default'
    state_domains: tuple[str, ...] = ()
    state_accesses: tuple[tuple[str, str, str], ...] = ()
    package_constraints: tuple[str, ...] = ()


@dataclass(frozen=True)
class TopologyPlan:
    strategy: str
    groups: tuple[tuple[str, ...], ...]
    topology_id: str
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    logical_to_physical: dict[str, str] = field(default_factory=dict)
    valid: bool = True
    blocking_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TopologyConstraints:
    max_groups: int = 32
    max_candidates: int = 8
    seed: int = 0
    allow_unknown_state_split: bool = False


_STRATEGIES = {'baseline', 'all_in_one', 'per_node', 'state_domain', 'affinity_cluster', 'dependency_partition'}


def _domains(node):
    return set(node.state_domains) | ({node.state_domain} if node.state_domain else set())


def _required_groups(nodes, baseline_groups, allow_unknown):
    groups = [{node.name} for node in nodes]
    for node in nodes:
        required = set(node.must_colocate)
        if node.state_backend in {'memory', 'in_process', 'process', 'declared', 'local'} and not node.shareable_state:
            required |= {other.name for other in nodes if _domains(node) & _domains(other)}
        groups = _merge_required(groups, {node.name: required | {node.name}})
    if not allow_unknown:
        by_name = {node.name: node for node in nodes}
        for baseline in baseline_groups or ():
            known = set(baseline) & by_name.keys()
            if any(not by_name[name].state_backend for name in known):
                groups = _merge_required(groups, {name: known for name in known})
    return {name: set(group) for group in groups for name in group}


def _merge_required(groups, required):
    changed = True
    while changed:
        changed = False
        for index, group in enumerate(groups):
            needed = set().union(*(required.get(name, {name}) for name in group))
            for other_index, other in enumerate(groups):
                if index != other_index and needed & other:
                    groups[index] |= other
                    groups.pop(other_index)
                    changed = True
                    break
            if changed:
                break
    return groups


def _valid_groups(groups, nodes):
    groups_list = [set(group) for group in groups]
    reasons = []
    actual = set().union(*groups_list) if groups_list else set()
    if actual != set(nodes):
        reasons.append(f'route_coverage_mismatch: missing={sorted(set(nodes)-actual)} unknown={sorted(actual-set(nodes))}')
    if sum(map(len, groups_list)) != len(actual):
        reasons.append('duplicate_node_route')
    for node in nodes.values():
        unknown = (set(node.must_colocate) | set(node.must_separate) | set(node.dependencies)) - nodes.keys()
        if unknown:
            reasons.append(f'unknown_node_reference: {node.name}: {sorted(unknown)}')
    for index, group in enumerate(groups_list):
        known = group & nodes.keys()
        for name in known:
            node = nodes[name]
            if set(node.must_separate) & group:
                reasons.append(f'must_separate_conflict: {name}: {sorted(set(node.must_separate)&group)}')
            if not set(node.must_colocate) <= group:
                reasons.append(f'must_colocate_split: {name}')
        versions = {nodes[name].python_version for name in known if nodes[name].python_version}
        if len(versions) > 1:
            reasons.append(f'python_version_conflict: group {index}: {sorted(versions)}')
        isolation = {nodes[name].isolation_class for name in known}
        if len(isolation) > 1 or ('strict' in isolation and len(group) > 1):
            reasons.append(f'isolation_conflict: group {index}')
        if len(group) > 1 and any(not nodes[name].parallel_safe for name in known):
            reasons.append(f'parallel_safety_unverified: group {index}')
        # Equal pinned packages are statically checkable; all other ranges are
        # retained for an isolated build probe rather than labelled compatible.
        pins = {}
        for name in known:
            for requirement in nodes[name].package_constraints:
                match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^;\s]+)', requirement.strip())
                if match:
                    pins.setdefault(match[1].lower().replace('_', '-'), set()).add(match[2])
        if any(len(values) > 1 for values in pins.values()):
            reasons.append(f'package_version_conflict: group {index}')
    return not reasons, sorted(set(reasons))


def generate_topology(nodes: Iterable[TopologyNode], strategy='baseline', *, baseline_groups=None, constraints=None):
    if strategy not in _STRATEGIES:
        raise ValueError(f'unknown topology strategy: {strategy}')
    options = constraints or TopologyConstraints()
    ordered = sorted(nodes, key=lambda node: node.name)
    by_name = {node.name: node for node in ordered}
    baseline = tuple(tuple(group) for group in baseline_groups) if baseline_groups else ()
    warnings = []
    if any(not node.state_backend for node in ordered):
        warnings.append('unknown_state_semantics')
    if any(node.package_constraints or node.system_dependencies for node in ordered):
        warnings.append('dependency_compatibility_requires_isolated_build')
    if strategy == 'baseline':
        groups = [set(group) for group in baseline] if baseline else [{node.name} for node in ordered]
    elif strategy == 'all_in_one':
        groups = [set(by_name)]
    elif strategy in {'state_domain', 'affinity_cluster'}:
        groups = [{node.name} for node in ordered]
        for node in ordered:
            related = set(node.dependencies) if strategy == 'affinity_cluster' else {other.name for other in ordered if _domains(node) & _domains(other)}
            groups = _merge_required(groups, {node.name: related | {node.name}})
    elif strategy == 'dependency_partition':
        buckets = {}
        for node in ordered:
            key = (node.preferred_resource_class, node.python_version, tuple(sorted(node.system_dependencies)))
            buckets.setdefault(key, set()).add(node.name)
        groups = list(buckets.values())
    else:
        groups = [{node.name} for node in ordered]
    if strategy != 'baseline':
        groups = _merge_required(groups, _required_groups(ordered, baseline, options.allow_unknown_state_split))
    canonical = tuple(sorted(tuple(sorted(group)) for group in groups if group))
    valid, reasons = _valid_groups(canonical, by_name)
    if strategy != 'baseline':
        reasons.extend(validate_state_regrouping(ordered, canonical, baseline))
    if strategy == 'baseline' and baseline and sum(map(len, baseline)) != sum(len(set(group)) for group in baseline):
        reasons.append('duplicate_node_route')
    if len(by_name) != len(ordered):
        reasons.append('duplicate_node_definition')
    if len(canonical) > options.max_groups:
        reasons.append('max_groups_exceeded')
    mapping = {name: f'server_{index}' for index, group in enumerate(canonical) for name in group}
    return TopologyPlan(strategy, canonical, 'topology-' + digest({'groups': canonical}),
                        (f'strategy={strategy}',), tuple(warnings), mapping, valid and not reasons,
                        tuple(reasons), tuple(warnings), (strategy,))


def generate_topologies(nodes, *, strategies=None, baseline_groups=None, constraints=None):
    options = constraints or TopologyConstraints()
    nodes = list(nodes)
    result = {}
    for strategy in strategies or ('baseline', 'all_in_one', 'per_node', 'state_domain', 'affinity_cluster', 'dependency_partition'):
        plan = generate_topology(nodes, strategy, baseline_groups=baseline_groups, constraints=options)
        if not plan.valid:
            continue
        if plan.topology_id in result:
            prior = result[plan.topology_id]
            result[plan.topology_id] = replace(prior, strategies=prior.strategies + (strategy,))
        elif len(result) < options.max_candidates:
            result[plan.topology_id] = plan
    return list(result.values())


def validate_state_regrouping(nodes, groups, baseline_groups):
    canonical = tuple(sorted(tuple(sorted(group)) for group in groups))
    baseline = tuple(sorted(tuple(sorted(group)) for group in baseline_groups))
    if not baseline or canonical == baseline:
        return []
    by_name = {node.name: node for node in nodes}
    def domains(group):
        return set().union(*(_domains(by_name[name]) for name in group if name in by_name))
    blockers = []
    for original in baseline:
        changed = original not in canonical
        if changed and any(not by_name[name].state_backend for name in original if name in by_name):
            blockers.append('unknown_state_group_changed')
        if changed and len(domains(original)) > 1:
            blockers.append('ambiguous_state_regrouping')
    if any(group not in baseline and len(domains(group)) > 1 for group in canonical):
        blockers.append('ambiguous_state_regrouping')
    return sorted(set(blockers))


def normalize_config(config, config_path=None):
    normalized = copy.deepcopy(config)
    if 'pipeline' in normalized and 'realizations' in normalized:
        raise ValueError('ambiguous pipeline and realizations')
    pipeline = normalized.get('pipeline', normalized.pop('realizations', None))
    if not isinstance(pipeline, dict) or not set(pipeline) <= {'faas', 'session', 'mcp_serverless'}:
        raise ValueError('unsupported pipeline format')
    if 'session' in pipeline and 'mcp_serverless' in pipeline:
        raise ValueError('ambiguous session and mcp_serverless')
    if 'mcp_serverless' in pipeline:
        pipeline['session'] = pipeline.pop('mcp_serverless')
    normalized['pipeline'] = pipeline
    if 'nodes' not in normalized and not normalized.get('tools'):
        raise ValueError('unsupported node format')
    if not normalized.get('nodes') and normalized.get('tools') and config_path is not None:
        path = Path(config_path).resolve()
        normalized['nodes'] = _nodes_from_tools(path, resolve_benchmark_root(path), normalized.pop('tools'))
    return normalized


def nodes_from_config(config_path, provider='aws_lambda'):
    path = Path(config_path).resolve()
    config = normalize_config(json.loads(path.read_text()), path)
    resolved = resolve_benchmark(path, provider)
    raw_by_name = {str(raw.get('name')): raw for raw in config.get('nodes', [])}
    nodes = []
    for name, node in resolved.nodes.items():
        raw = raw_by_name.get(name, {})
        metadata = {**raw, **raw.get('topology', {})}
        accesses = tuple((trace, node.access_for_trace(trace).domain, node.access_for_trace(trace).access) for trace in node.trace_names)
        domains = tuple(sorted({domain for _, domain, access in accesses if domain and access != 'stateless'}))
        declared = raw.get('state', {})
        explicit = all(isinstance(binding, dict) and binding.get('state_semantics_declared', True)
                       and ('access' in binding or 'stateful' in binding) for binding in raw.get('trace_names', [])) and bool(raw.get('trace_names'))
        backend = declared.get('backend', metadata.get('state_backend', 'declared' if domains else 'stateless' if explicit else ''))
        requirements = node.dir / 'requirements.txt'
        packages = tuple(line.strip() for line in requirements.read_text().splitlines() if line.strip() and not line.startswith('#')) if requirements.is_file() else ()
        def values(key):
            return tuple(str(item) for item in metadata.get(key, ()))
        build = node.build if node.build.python_version else node.mcp_build if resolved.family == 'session' else node.function_build
        nodes.append(TopologyNode(name, domains[0] if len(domains) == 1 else '', backend,
            bool(declared.get('shareable', metadata.get('shareable_state', backend == 'stateless'))),
            values('must_colocate'), values('must_separate'), values('dependencies'), build.python_version,
            values('system_dependencies'), values('side_effects'), values('required_permissions'),
            str(metadata.get('isolation_class', 'default')), bool(metadata.get('parallel_safe', True)),
            str(metadata.get('preferred_resource_class', 'default')), domains, accesses, packages))
    return nodes, tuple(tuple(target.node_names) for target in resolved.targets.values())


def compile_session_config(config_path, plan, *, output_path=None):
    path = Path(config_path).resolve()
    compiled = normalize_config(json.loads(path.read_text()), path)
    nodes, baseline = nodes_from_config(path, 'gcp_mcp')
    valid, reasons = _valid_groups(plan.groups, {node.name: node for node in nodes})
    # Re-evaluate unknown-state and process-state atoms at the compile boundary.
    required = _required_groups(nodes, baseline, False)
    reasons.extend(validate_state_regrouping(nodes, plan.groups, baseline))
    for group in plan.groups:
        for name in group:
            if not required.get(name, {name}) <= set(group):
                reasons.append(f'state_atom_split: {name}')
    if not plan.valid or not valid or reasons or not plan.groups:
        raise ValueError('cannot compile invalid topology: ' + '; '.join((*plan.blocking_reasons, *reasons)))
    base = compiled['pipeline']['session']
    servers = []
    for index, group in enumerate(plan.groups):
        existing = next((server for server in base['servers'] if set(server.get('nodes', [])) == set(group)), None)
        if existing:
            server = copy.deepcopy(existing)
        else:
            server = copy.deepcopy(base['servers'][0])
            server['name'] = f'server_{index}'
        server['nodes'] = list(group)
        servers.append(server)
    mapping = {name: server['name'] for server in servers for name in server['nodes']}
    compiled['pipeline']['session'] = {**copy.deepcopy(base), 'servers': servers}
    original = resolve_benchmark(path, 'gcp_mcp')
    compiled['recommender'] = {'topology_id': plan.topology_id, 'strategies': plan.strategies,
        'logical_to_physical': mapping, 'source_config_digest': file_digest(path),
        'baseline_state_domains': {name: logical_state_domain(original, name) for name in original.targets}}
    if output_path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(compiled, indent=2, ensure_ascii=False) + '\n')
    return compiled


def validate_topology(plan, node_names):
    expected = set(node_names)
    actual = {name for group in plan.groups for name in group}
    reasons = list(plan.blocking_reasons)
    if not plan.valid and not reasons:
        reasons.append('invalid_topology')
    if actual != expected:
        reasons.append(f'route coverage mismatch: expected={sorted(expected)} actual={sorted(actual)}')
    if len(actual) != sum(len(group) for group in plan.groups):
        reasons.append('a node appears in multiple topology groups')
    return reasons
