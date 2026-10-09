from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from atsuite.cli.build_docker_images import build_resolved_targets
from atsuite.execution.workers import run_parallel
from atsuite.recommender.identity import digest
from atsuite.recommender.identity import tree_digest, file_digest


def build_key(candidate: dict[str, Any]) -> str:
    config_path = Path(candidate.get('generated_config', ''))
    source_digest = candidate.get('bundle_digest') or candidate.get('workload_fingerprint')
    if config_path.is_file():
        from atsuite.pipeline import resolve_benchmark
        resolved = resolve_benchmark(config_path, candidate['provider'])
        source_digest = digest({'nodes':{name:tree_digest(node.dir)[0] for name,node in resolved.nodes.items()},
                                'data': tree_digest(resolved.bench_root / 'data')[0] if (resolved.bench_root / 'data').exists() else '',
                                'bundle':source_digest},length=64)
    root=Path(__file__).resolve().parents[2]
    return "build-group-" + digest({
        "provider": candidate["provider"], "family": candidate["family"],
        "topology": candidate["logical_topology"], "architecture": candidate["runtime_architecture"],
        'source':source_digest,'sdk':tree_digest(root / 'atsuite_sdk')[0],
        'dockerfiles':tree_digest(root / 'dockerfiles')[0],
        'dependency_lock':file_digest(root / 'uv.lock'),
        'base_image_digest':candidate.get('base_image_digest',''),
    }, length=24)


def build_candidates(plan: dict[str, Any], output_root: str | Path, *, max_workers: int = 1,
                     builder: Callable[..., dict[str, Any]] = build_resolved_targets) -> dict[str, dict[str, Any]]:
    """Build one isolated image set per provider/topology/architecture group."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in plan.get("candidates", []):
        if not candidate.get("generated_config"):
            raise ValueError(f"candidate {candidate.get('candidate_id')} has no generated_config; materialize the plan first")
        groups.setdefault(build_key(candidate), []).append(candidate)
    items = sorted(groups.items())

    def build(item):
        key, candidates = item
        representative = candidates[0]
        artifacts = builder(
            Path(representative["generated_config"]), representative["provider"], str(Path(output_root).resolve()),
            platform=representative["runtime_architecture"], experiment_id=str(plan.get("experiment_id") or ""), candidate_id=key,
        )
        return key, artifacts

    built = dict(run_parallel(items, build, max_workers=max_workers))
    return {candidate["candidate_id"]: built[build_key(candidate)] for candidate in plan.get("candidates", [])}
