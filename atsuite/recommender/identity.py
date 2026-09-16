from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def canonicalize(value: Any) -> Any:
    if is_dataclass(value):
        return canonicalize(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): canonicalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        items = [canonicalize(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))) if isinstance(value, set) else items
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any, *, length: int = 16) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


def file_digest(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def tree_digest(root: str | Path, *, ignore_names: set[str] | None = None) -> tuple[str, list[str]]:
    """Hash a directory by relative path, file mode, and bytes without following symlinks."""
    base = Path(root).resolve()
    ignored = ignore_names or {"__pycache__", ".DS_Store"}
    hasher = hashlib.sha256()
    files: list[str] = []
    for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()):
        relative = path.relative_to(base).as_posix()
        if any(part in ignored for part in path.relative_to(base).parts):
            continue
        if path.is_symlink():
            hasher.update(f"L {relative} {path.readlink()}\n".encode())
            files.append(relative)
        elif path.is_file():
            hasher.update(f"F {relative} {path.stat().st_mode & 0o777:o} ".encode())
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            hasher.update(b"\n")
            files.append(relative)
    return hasher.hexdigest(), files


def workload_fingerprint(config_path: str | Path, trace_path: str | Path | None = None) -> str:
    payload = {"config": file_digest(config_path)}
    if trace_path:
        payload["trace"] = file_digest(trace_path)
    return digest(payload, length=32)


def build_id(*, source_digest: str, dependency_lock_digest: str = "", dockerfile_digest: str = "", base_digest: str = "", topology: Any = None, platform: str = "", build_args: dict[str, Any] | None = None) -> str:
    return "build-" + digest({"source_digest": source_digest, "dependency_lock_digest": dependency_lock_digest,
                               "dockerfile_digest": dockerfile_digest, "base_digest": base_digest,
                               "topology": topology, "platform": platform, "build_args": build_args or {}})


def candidate_id(*, workload_fingerprint: str, provider: str, family: str, deployment_mode: str, region: str,
                 topology: Any, resources_by_target: Any, runtime_architecture: str, state_policy: str, lifecycle_policy: str) -> str:
    return "candidate-" + digest(locals())


def deployment_id(experiment_id: str, candidate: Any, account_profile: str, namespace: str) -> str:
    candidate_value = candidate.candidate_id if hasattr(candidate, "candidate_id") else candidate
    return "deployment-" + digest({"experiment_id": experiment_id, "candidate_id": candidate_value,
                                    "account_profile": account_profile, "namespace": namespace})


def run_id(experiment_id: str, candidate_id_value: str, scenario_id: str, block_id: str, index: int) -> str:
    return "run-" + digest({"experiment_id": experiment_id, "candidate_id": candidate_id_value,
                             "scenario_id": scenario_id, "block_id": block_id, "index": index})
