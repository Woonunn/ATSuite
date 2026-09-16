from __future__ import annotations

import copy
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from atsuite.pipeline import resolve_benchmark
from atsuite.recommender.identity import digest, file_digest


@dataclass(frozen=True)
class TopologyNode:
    name: str
    state_domain: str = ""
    state_backend: str = ""
    shareable_state: bool = False
    must_colocate: tuple[str, ...] = ()
    must_separate: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    python_version: str = ""
    system_dependencies: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    isolation_class: str = "default"
    parallel_safe: bool = True
    preferred_resource_class: str = "default"


@dataclass(frozen=True)
class TopologyPlan:
    strategy: str
    groups: tuple[tuple[str, ...], ...]
    topology_id: str
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    logical_to_physical: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TopologyConstraints:
    max_groups: int = 32
    max_candidates: int = 8
    seed: int = 0
    allow_unknown_state_split: bool = False


_STRATEGIES = {"baseline", "all_in_one", "per_node", "state_domain", "affinity_cluster", "dependency_partition"}


def _canon_nodes(nodes: Iterable[TopologyNode]) -> list[TopologyNode]:
    return sorted(nodes, key=lambda node: node.name)


def _must_colocate(nodes: list[TopologyNode]) -> dict[str, set[str]]:
    parent = {node.name: node.name for node in nodes}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        if left in parent and right in parent:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

    for node in nodes:
        for other in node.must_colocate:
            union(node.name, other)
    groups: dict[str, set[str]] = {}
    for name in parent:
        groups.setdefault(find(name), set()).add(name)
    return groups


def _valid_groups(groups: Iterable[Iterable[str]], nodes: dict[str, TopologyNode]) -> tuple[bool, list[str]]:
    groups_list = [set(group) for group in groups]
    reasons = []
    for index, group in enumerate(groups_list):
        for name in group:
            node = nodes[name]
            if set(node.must_separate) & group:
                reasons.append(f"{name} must be separate from {sorted(set(node.must_separate) & group)}")
            if any(nodes[dependency].isolation_class != node.isolation_class for dependency in node.dependencies if dependency in nodes and dependency in group):
                reasons.append(f"{name} has incompatible dependency isolation")
        versions = {nodes[name].python_version for name in group if nodes[name].python_version}
        if len(versions) > 1:
            reasons.append(f"python versions conflict in group {index}: {sorted(versions)}")
        permissions = {permission for name in group for permission in nodes[name].required_permissions}
        if any(nodes[name].isolation_class == "strict" and len(group) > 1 for name in group):
            reasons.append(f"strict isolation forbids group {sorted(group)}")
        if len(permissions) > 32:
            reasons.append(f"permission set too large in group {index}")
    return not reasons, sorted(set(reasons))


def _merge_required(groups: list[set[str]], required: dict[str, set[str]]) -> list[set[str]]:
    changed = True
    while changed:
        changed = False
        for index, group in enumerate(groups):
            needed = set().union(*(required.get(name, set()) for name in group))
            for other_index, other in enumerate(groups):
                if index != other_index and needed & other:
                    groups[index] |= other
                    groups.pop(other_index)
                    changed = True
                    break
            if changed:
                break
    return groups


def _base_groups(nodes: list[TopologyNode], strategy: str, baseline_groups: Iterable[Iterable[str]] | None = None) -> tuple[list[set[str]], list[str]]:
    limitations: list[str] = []
    by_name = {node.name: node for node in nodes}
    if strategy == "baseline":
        if baseline_groups:
            return [set(group) for group in baseline_groups], limitations
        limitations.append("baseline grouping was not supplied; using one node per group")
        return [{node.name} for node in nodes], limitations
    if strategy == "per_node":
        groups = [{node.name} for node in nodes]
    elif strategy == "all_in_one":
        groups = [set(node.name for node in nodes)]
    elif strategy == "state_domain":
        domains: dict[str, set[str]] = {}
        groups = []
        for node in nodes:
            key = node.state_domain if node.state_domain and node.state_backend and not node.shareable_state else f"__{node.name}"
            domains.setdefault(key, set()).add(node.name)
        groups = list(domains.values())
    elif strategy == "dependency_partition":
        buckets: dict[tuple[str, tuple[str, ...]], set[str]] = {}
        for node in nodes:
            key = (node.preferred_resource_class, tuple(sorted(node.system_dependencies)))
            buckets.setdefault(key, set()).add(node.name)
        groups = list(buckets.values())
    elif strategy == "affinity_cluster":
        groups = [{node.name} for node in nodes]
        for node in nodes:
            for dependency in node.dependencies:
                if dependency in by_name:
                    left = next(group for group in groups if node.name in group)
                    right = next(group for group in groups if dependency in group)
                    if left is not right:
                        left |= right
                        groups.remove(right)
        limitations.append("affinity_cluster uses declared dependency affinity; trace frequency is not inferred")
    else:
        raise ValueError(f"unknown topology strategy: {strategy}")
    required = _must_colocate(nodes)
    groups = _merge_required(groups, {name: set(group) for group in required.values() for name in group})
    if not all(node.state_domain and node.state_backend for node in nodes):
        limitations.append("state declarations are incomplete; destructive state splitting was avoided")
    return groups, limitations


def generate_topology(nodes: Iterable[TopologyNode], strategy: str = "baseline", *, baseline_groups: Iterable[Iterable[str]] | None = None, constraints: TopologyConstraints | None = None) -> TopologyPlan:
    if strategy not in _STRATEGIES:
        raise ValueError(f"unknown topology strategy: {strategy}")
    options = constraints or TopologyConstraints()
    ordered = _canon_nodes(nodes)
    by_name = {node.name: node for node in ordered}
    groups, limitations = _base_groups(ordered, strategy, baseline_groups)
    groups = [set(group) for group in groups if group]
    valid, reasons = _valid_groups(groups, by_name)
    if not valid:
        return TopologyPlan(strategy, tuple(tuple(sorted(group)) for group in sorted(groups, key=lambda group: sorted(group))),
                            "topology-" + digest({"strategy": strategy, "groups": groups}), tuple(), tuple(limitations + reasons), {})
    if len(groups) > options.max_groups:
        limitations.append(f"group count {len(groups)} exceeds max_groups={options.max_groups}")
        return TopologyPlan(strategy, tuple(), "", tuple(), tuple(limitations), {})
    canonical = tuple(tuple(sorted(group)) for group in sorted(groups, key=lambda group: tuple(sorted(group))))
    mapping = {name: f"server-{index}" for index, group in enumerate(canonical) for name in group}
    return TopologyPlan(strategy, canonical, "topology-" + digest({"strategy": strategy, "groups": canonical}),
                        (f"strategy={strategy}",), tuple(sorted(set(limitations))), mapping)


def generate_topologies(nodes: Iterable[TopologyNode], *, strategies: Iterable[str] | None = None, baseline_groups: Iterable[Iterable[str]] | None = None, constraints: TopologyConstraints | None = None) -> list[TopologyPlan]:
    options = constraints or TopologyConstraints()
    seen = set()
    result = []
    for strategy in strategies or ("baseline", "all_in_one", "per_node", "state_domain", "affinity_cluster", "dependency_partition"):
        plan = generate_topology(nodes, strategy, baseline_groups=baseline_groups, constraints=options)
        if plan.topology_id and plan.topology_id not in seen:
            result.append(plan)
            seen.add(plan.topology_id)
        if len(result) >= options.max_candidates:
            break
    return result


def nodes_from_config(config_path: str | Path, provider: str = "aws_lambda") -> tuple[list[TopologyNode], tuple[tuple[str, ...], ...]]:
    resolved = resolve_benchmark(Path(config_path).resolve(), provider)
    nodes = []
    for name, node in resolved.nodes.items():
        accesses = [node.access_for_trace(trace_name) for trace_name in node.trace_names]
        domains = {access.domain for access in accesses if access.is_stateful and access.domain}
        nodes.append(TopologyNode(name=name, state_domain=sorted(domains)[0] if len(domains) == 1 else "",
                                  state_backend="declared" if domains else "", shareable_state=not domains))
    baseline = []
    for target in resolved.targets.values():
        baseline.append(tuple(target.node_names))
    return nodes, tuple(baseline)


def compile_session_config(config_path: str | Path, plan: TopologyPlan, *, output_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path).resolve()
    config = __import__("json").loads(path.read_text(encoding="utf-8"))
    if not plan.groups:
        raise ValueError("cannot compile invalid topology")
    compiled = copy.deepcopy(config)
    pipeline = compiled.setdefault("pipeline", {})
    base = pipeline.get("session") or pipeline.get("mcp_serverless") or {}
    servers = []
    template = copy.deepcopy((base.get("servers") or [{}])[0])
    for index, group in enumerate(plan.groups):
        existing = next((server for server in base.get("servers", []) if set(server.get("nodes", [])) == set(group)), {})
        server = copy.deepcopy(template)
        server.update(copy.deepcopy(existing))
        server["name"] = f"server_{index}"
        server["nodes"] = list(group)
        servers.append(server)
    pipeline["session"] = {**copy.deepcopy(base), "servers": servers}
    pipeline.pop("mcp_serverless", None)
    compiled["recommender"] = {"topology_id": plan.topology_id, "strategy": plan.strategy,
                                 "logical_to_physical": plan.logical_to_physical,
                                 "source_config_digest": file_digest(path)}
    if output_path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(__import__("json").dumps(compiled, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return compiled


def validate_topology(plan: TopologyPlan, node_names: Iterable[str]) -> list[str]:
    expected = set(node_names)
    actual = {name for group in plan.groups for name in group}
    reasons = []
    if actual != expected:
        reasons.append(f"route coverage mismatch: expected={sorted(expected)} actual={sorted(actual)}")
    if len(actual) != sum(len(group) for group in plan.groups):
        reasons.append("a node appears in multiple topology groups")
    return reasons
