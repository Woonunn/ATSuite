from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from atsuite.pipeline import PROVIDERS, resolve_benchmark
from atsuite.recommender.capabilities import describe_capability, enumerate_legal_resources
from atsuite.recommender.contracts import CandidateSpec, ExperimentSpec, utc_now
from atsuite.recommender.identity import candidate_id, file_digest, workload_fingerprint
from atsuite.recommender.topology import TopologyPlan, compile_session_config, nodes_from_config, generate_topologies


def _candidate_from_resolved(spec: ExperimentSpec, config_path: Path, provider: str, region: str = "", topology=None, resource_variant: int = 0) -> CandidateSpec:
    resolved = resolve_benchmark(config_path, provider)
    if topology is None:
        groups = tuple(tuple(target.node_names) for target in resolved.targets.values())
        strategy = "baseline"
        topology_id = "baseline"
    else:
        groups = topology.groups
        strategy = topology.strategy
        topology_id = topology.topology_id
    capability = describe_capability(provider, region, resolved.family)
    legal = enumerate_legal_resources(provider, region, resolved.family)
    if not legal:
        raise ValueError(f"no legal resource vector for {provider}")
    # Keep the resource search bounded and deterministic. The execution stage
    # can replace this with coordinate search after capability readback.
    choices = [legal[0], legal[len(legal) // 2], legal[-1]]
    vector = choices[min(resource_variant, len(choices) - 1)]
    resources: dict[str, dict[str, Any]] = {}
    if resolved.family == "session" and topology is not None:
        target_names = [f"server_{index}" for index, _ in enumerate(groups)]
    else:
        target_names = list(resolved.targets)
    for target_name in target_names:
        resources[target_name] = vector.to_dict()
    workload = workload_fingerprint(config_path, resolved.trace_path)
    identity_kwargs = {"workload_fingerprint": workload, "provider": provider, "family": resolved.family,
                       "deployment_mode": resolved.family, "region": region or "default",
                       "topology": {"strategy": strategy, "topology_id": topology_id, "groups": groups},
                       "resources_by_target": resources, "runtime_architecture": vector.architecture,
                       "state_policy": "isolated_uid", "lifecycle_policy": "ephemeral"}
    generated_id = candidate_id(**identity_kwargs)
    return CandidateSpec(generated_id, workload, provider, resolved.family, resolved.family, region or "default",
                         vector.architecture, groups, resources, "isolated_uid", "ephemeral",
                         capability.snapshot_id, file_digest(config_path))


def plan_experiment(spec: ExperimentSpec) -> dict[str, Any]:
    config_path = Path(spec.benchmark_config).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(config_path)
    # Parse the benchmark once with the first provider that actually supports
    # the configured family; an invalid optional provider should be an exclusion.
    parse_provider = None
    for provider in spec.providers:
        try:
            resolve_benchmark(config_path, provider)
            parse_provider = provider
            break
        except (ValueError, SystemExit):
            continue
    if parse_provider is None:
        raise ValueError("none of the requested providers can resolve this benchmark")
    nodes, baseline = nodes_from_config(config_path, parse_provider)
    topologies = generate_topologies(
        nodes, strategies=spec.search.topology_strategies, baseline_groups=baseline,
        constraints=__import__("atsuite.recommender.topology", fromlist=["TopologyConstraints"]).TopologyConstraints(
            max_candidates=spec.search.max_generated_topologies
        ),
    )
    candidates = []
    capabilities = {}
    exclusions = []
    candidate_limit = spec.search.max_candidates
    for provider in spec.providers:
        try:
            resolved = resolve_benchmark(config_path, provider)
            region = spec.regions_by_provider.get(provider, "default")
            capabilities[provider] = asdict(describe_capability(provider, region, resolved.family))
            provider_topologies = topologies if resolved.family == "session" else [None]
            for topology in provider_topologies:
                for resource_variant in range(3):
                    if len(candidates) >= candidate_limit:
                        break
                    try:
                        candidate = _candidate_from_resolved(spec, config_path, provider, region=region, topology=topology, resource_variant=resource_variant)
                        if not any(row["candidate_id"] == candidate.candidate_id for row in candidates):
                            candidates.append(asdict(candidate))
                    except (ValueError, SystemExit) as exc:
                        exclusions.append({"provider": provider, "reason": str(exc)})
                if len(candidates) >= candidate_limit:
                    break
        except (ValueError, SystemExit) as exc:
            exclusions.append({"provider": provider, "reason": str(exc)})
    return {"schema_version": 1, "status": "planned", "created_at": utc_now(), "spec": spec.to_dict(),
            "scope": {"workload_fingerprint": workload_fingerprint(config_path), "search_exhaustive": False,
                       "generated_candidates": len(candidates), "measured_candidates": 0, "confirmed_candidates": 0},
            "candidates": candidates, "capabilities": capabilities, "excluded": exclusions,
            "topologies": [topology.to_dict() for topology in topologies], "promising_unverified": [],
            "best_verified": None, "advisor_alternative": None}


def _absolute_benchmark_references(config: dict[str, Any], source: Path) -> dict[str, Any]:
    from atsuite.utils import resolve_benchmark_root
    root = resolve_benchmark_root(source)
    trace = config.get("trace_file")
    if trace:
        config["trace_file"] = str((root / str(trace)).resolve()) if not Path(str(trace)).is_absolute() else str(trace)
    for node in config.get("nodes", []):
        directory = node.get("dir")
        if directory:
            node["dir"] = str((root / str(directory)).resolve()) if not Path(str(directory)).is_absolute() else str(directory)
    return config


def _apply_candidate_resources(config: dict[str, Any], candidate: dict[str, Any]) -> None:
    pipeline = config["pipeline"]
    family = candidate["family"]
    targets = pipeline["faas"]["units"] if family == "faas" else pipeline["session"]["servers"]
    for target in targets:
        raw = candidate["resources_by_target"].get(target["name"])
        if not raw:
            continue
        target["deploy"] = {"cpu": raw["cpu"], "memory": raw["memory_mb"], "disk": raw.get("disk_mb", 512),
                            "timeout": raw.get("timeout_s", 30)}


def materialize_plan(plan: dict[str, Any], output: str | Path) -> Path:
    path = Path(output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    source = Path(plan["spec"]["benchmark"]["config"]).expanduser().resolve()
    source_config = json.loads(source.read_text(encoding="utf-8"))
    topology_by_groups = {tuple(tuple(group) for group in row["groups"]): row for row in plan.get("topologies", [])}
    candidates_root = path.parent / "candidates"
    for candidate in plan.get("candidates", []):
        candidate_dir = candidates_root / candidate["candidate_id"]
        generated = candidate_dir / "generated_config.json"
        groups = tuple(tuple(group) for group in candidate["logical_topology"])
        if candidate["family"] == "session":
            row = topology_by_groups.get(groups)
            if row is None:
                raise ValueError(f"missing topology for candidate {candidate['candidate_id']}")
            topology = TopologyPlan(row["strategy"], groups, row["topology_id"], tuple(row.get("reasons", ())),
                                    tuple(row.get("limitations", ())), dict(row.get("logical_to_physical", {})))
            config = compile_session_config(source, topology)
        else:
            config = json.loads(json.dumps(source_config))
        _absolute_benchmark_references(config, source)
        _apply_candidate_resources(config, candidate)
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(json.dumps(config, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        candidate["generated_config"] = str(generated)
        candidate["source_config"] = str(source)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n", encoding="utf-8")
    return path


def write_plan(spec: ExperimentSpec, output: str | Path) -> Path:
    return materialize_plan(plan_experiment(spec), output)
