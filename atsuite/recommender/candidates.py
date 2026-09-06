from pathlib import Path

from atsuite.pipeline import PROVIDERS, provider_cloud_name, resolve_benchmark
from atsuite.recommender.model import DeploymentCandidate


def generate_candidates(config_path: Path, requested_providers: list[str] | None = None) -> list[DeploymentCandidate]:
    providers = requested_providers if requested_providers is not None else [
        name for name in PROVIDERS if name != "mcp_gateway"
    ]
    candidates = []
    for provider in dict.fromkeys(name.strip().lower() for name in providers):
        spec = PROVIDERS.get(provider)
        reason = ""
        shape = {}
        try:
            resolved = resolve_benchmark(Path(config_path).resolve(), provider)
            targets = [
                {"name": target.name, "cpu": target.runtime.cpu,
                 "memory_mb": target.runtime.memory, "disk_mb": target.runtime.disk,
                 "timeout_s": target.runtime.timeout, "nodes": list(target.node_names)}
                for target in resolved.targets.values()
            ]
            shape = {
                "target_count": len(targets),
                "total_cpu": sum(target["cpu"] for target in targets),
                "total_memory_mb": sum(target["memory_mb"] for target in targets),
                "targets": targets,
            }
        except (Exception, SystemExit) as exc:
            reason = str(exc) or type(exc).__name__
        candidates.append(DeploymentCandidate(
            id=provider, provider=provider, cloud=provider_cloud_name(provider),
            family=spec.family if spec else "", config_path=Path(config_path).resolve(),
            supported=not reason, unsupported_reason=reason, deployment_shape=shape,
        ))
    return candidates
