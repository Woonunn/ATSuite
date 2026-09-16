from __future__ import annotations

from typing import Any, Mapping


def workload_features(workload: Mapping[str, Any]) -> dict[str, float]:
    numeric = {key: workload.get(key) for key in ("total_nodes", "tool_use_count", "dag_depth", "max_parallelism", "tool_call_density", "stateful_tool_ratio", "stateful_invocation_ratio", "rw_ratio")}
    return {key: float(value) for key, value in numeric.items() if isinstance(value, (int, float))}


def candidate_features(candidate: Any) -> dict[str, float]:
    resources = getattr(candidate, "resources_by_target", {}) or {}
    values = [resource for resource in resources.values() if isinstance(resource, Mapping)]
    return {
        "target_count": float(len(values)),
        "total_cpu": sum(float(value.get("cpu", 0)) for value in values),
        "total_memory_mb": sum(float(value.get("memory_mb", 0)) for value in values),
        "total_disk_mb": sum(float(value.get("disk_mb", 0)) for value in values),
        "family_session": float(getattr(candidate, "family", "") == "session"),
        "arch_arm64": float("arm64" in str(getattr(candidate, "runtime_architecture", ""))),
    }


def make_features(workload: Mapping[str, Any], candidate: Any, *, pilot: Mapping[str, Any] | None = None) -> dict[str, float]:
    result = {f"workload.{key}": value for key, value in workload_features(workload).items()}
    result.update({f"candidate.{key}": value for key, value in candidate_features(candidate).items()})
    if pilot:
        result.update({f"pilot.{key}": float(value) for key, value in pilot.items() if isinstance(value, (int, float))})
    return result
