from __future__ import annotations

from dataclasses import dataclass, field, asdict
import math
from typing import Any, Protocol

from atsuite.pipeline import PROVIDERS
from atsuite.recommender.contracts import CandidateSpec
from atsuite.recommender.identity import digest


@dataclass(frozen=True)
class ResourceVector:
    cpu: float
    memory_mb: int
    disk_mb: int = 512
    timeout_s: int = 30
    architecture: str = "linux/amd64"
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not math.isfinite(float(self.cpu)) or self.cpu <= 0 or self.memory_mb <= 0 or self.disk_mb <= 0 or self.timeout_s <= 0:
            raise ValueError("resource values must be positive")
        if int(self.memory_mb) != self.memory_mb or int(self.disk_mb) != self.disk_mb or int(self.timeout_s) != self.timeout_s:
            raise ValueError("memory, disk, and timeout must be integers")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceConstraints:
    memory_values: tuple[int, ...] | None = None
    cpu_values: tuple[float, ...] | None = None
    architectures: tuple[str, ...] | None = None
    min_memory_mb: int = 128
    max_memory_mb: int = 10240
    disk_mb: int = 512
    timeout_s: int = 30


@dataclass(frozen=True)
class CapabilitySnapshot:
    snapshot_id: str
    provider: str
    region: str
    deployment_mode: str
    family: str
    source: str
    version: str
    cpu_mode: str
    memory_values: tuple[int, ...]
    cpu_values: tuple[float, ...]
    legal_cpu_memory: tuple[tuple[float, int], ...]
    architectures: tuple[str, ...]
    disk_values: tuple[int, ...]
    timeout_range: tuple[int, int]
    session_affinity: bool
    supports_concurrency: bool
    supports_effective_readback: bool
    tunable_fields: tuple[str, ...]
    billing_modes: tuple[str, ...]

    @property
    def supports_fractional_cpu(self) -> bool:
        return any(cpu != int(cpu) for cpu in self.cpu_values)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reasons: tuple[str, ...] = ()
    effective: ResourceVector | None = None


class CapabilityProvider(Protocol):
    def describe(self, provider: str, region: str, deployment_mode: str = "") -> CapabilitySnapshot: ...
    def validate(self, candidate: CandidateSpec) -> ValidationResult: ...
    def enumerate_resources(self, constraints: ResourceConstraints, *, provider: str, region: str, deployment_mode: str = "") -> list[ResourceVector]: ...


def _snapshot(provider: str, region: str, mode: str, *, family: str, cpu_mode: str, memory: tuple[int, ...], cpu: tuple[float, ...], pairs: tuple[tuple[float, int], ...], architectures=("linux/amd64",), tunable=("memory",), session=False, concurrency=True, readback=False, disks=(512,), timeout=(1, 900), billing=("on_demand",)) -> CapabilitySnapshot:
    data = {"provider": provider, "region": region, "deployment_mode": mode, "family": family, "cpu_mode": cpu_mode,
            "memory_values": memory, "cpu_values": cpu, "legal_cpu_memory": pairs, "architectures": architectures,
            "disk_values": disks, "timeout_range": timeout, "session_affinity": session,
            "supports_concurrency": concurrency, "supports_effective_readback": readback, "tunable_fields": tunable,
            "billing_modes": billing}
    return CapabilitySnapshot("cap-" + digest(data), provider, region, mode, family, "rules", "1", cpu_mode, memory, cpu, pairs,
                             architectures, disks, timeout, session, concurrency, readback, tunable, billing)


class DefaultCapabilityProvider:
    """Provider rules used for planning; cloud API readback remains a runtime gate."""

    def describe(self, provider: str, region: str = "", deployment_mode: str = "") -> CapabilitySnapshot:
        provider = str(provider).lower().strip()
        spec = PROVIDERS.get(provider)
        if spec is None:
            raise ValueError(f"unknown provider: {provider}")
        region = region or "default"
        family = spec.family
        if provider == "aws_lambda":
            memory = tuple([128, 256, 512, 1024, 2048, 4096, 8192, 10240])
            pairs = tuple((round(memory_mb / 1769, 3), memory_mb) for memory_mb in memory)
            return _snapshot(provider, region, deployment_mode or "lambda", family=family, cpu_mode="derived",
                             memory=memory, cpu=tuple(sorted({pair[0] for pair in pairs})), pairs=pairs,
                             architectures=("linux/amd64", "linux/arm64"), tunable=("memory", "disk", "timeout"), readback=True)
        if provider == "aws_agentcore":
            return _snapshot(provider, region, deployment_mode or "agentcore", family=family, cpu_mode="not_tunable",
                             memory=(1024,), cpu=(1.0,), pairs=((1.0, 1024),), architectures=("linux/arm64",),
                             tunable=("timeout",), readback=True, session=True)
        if provider == "ali_agentrun":
            return _snapshot(provider, region, deployment_mode or "agentrun", family=family, cpu_mode="not_tunable",
                             memory=(1024,), cpu=(1.0,), pairs=((1.0, 1024),), architectures=("linux/amd64",),
                             tunable=("timeout",), readback=False, session=True)
        if provider == "mcp_gateway":
            return _snapshot(provider, region, deployment_mode or "gateway", family=family, cpu_mode="external",
                             memory=(1024,), cpu=(1.0,), pairs=((1.0, 1024),), tunable=(), readback=False, session=True)
        if provider.startswith("gcp"):
            memory = (128, 256, 512, 1024, 2048, 4096, 8192, 16384)
            cpu = (0.08, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
            pairs = tuple((c, m) for c in cpu for m in memory if m >= max(128, int(c * 512)))
            return _snapshot(provider, region, deployment_mode or "cloud_run", family=family, cpu_mode="independent",
                             memory=memory, cpu=cpu, pairs=pairs, architectures=("linux/amd64",),
                             tunable=("cpu", "memory", "disk", "timeout"), session=family == "session", readback=True,
                             timeout=(1, 3600))
        memory = (128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)
        cpu = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
        pairs = tuple((c, m) for c in cpu for m in memory if m >= int(c * 256))
        return _snapshot(provider, region, deployment_mode or "function_compute", family=family, cpu_mode="independent",
                         memory=memory, cpu=cpu, pairs=pairs, architectures=("linux/amd64",),
                         tunable=("cpu", "memory", "disk", "timeout"), session=family == "session", readback=True)

    def enumerate_resources(self, constraints: ResourceConstraints, *, provider: str, region: str = "", deployment_mode: str = "") -> list[ResourceVector]:
        snapshot = self.describe(provider, region, deployment_mode)
        memories = constraints.memory_values or tuple(v for v in snapshot.memory_values if constraints.min_memory_mb <= v <= constraints.max_memory_mb)
        cpus = constraints.cpu_values or snapshot.cpu_values
        archs = constraints.architectures or snapshot.architectures
        results = []
        for cpu, memory in snapshot.legal_cpu_memory:
            if cpu not in cpus or memory not in memories:
                continue
            for architecture in archs:
                if architecture in snapshot.architectures:
                    results.append(ResourceVector(cpu, memory, constraints.disk_mb, constraints.timeout_s, architecture))
        return sorted(results, key=lambda item: (item.memory_mb, item.cpu, item.architecture))

    def validate(self, candidate: CandidateSpec) -> ValidationResult:
        snapshot = self.describe(candidate.provider, candidate.region, candidate.deployment_mode)
        reasons = []
        for target, raw in candidate.resources_by_target.items():
            try:
                resource = ResourceVector(float(raw.get("cpu", 1)), int(raw.get("memory_mb", 1024)), int(raw.get("disk_mb", 512)),
                                          int(raw.get("timeout_s", 30)), str(raw.get("architecture", snapshot.architectures[0])))
            except (TypeError, ValueError) as exc:
                reasons.append(f"{target}: {exc}")
                continue
            if resource.architecture not in snapshot.architectures:
                reasons.append(f"{target}: architecture {resource.architecture} unsupported")
            if (resource.cpu, resource.memory_mb) not in snapshot.legal_cpu_memory:
                reasons.append(f"{target}: cpu/memory combination ({resource.cpu}, {resource.memory_mb}) unsupported")
            if not snapshot.timeout_range[0] <= resource.timeout_s <= snapshot.timeout_range[1]:
                reasons.append(f"{target}: timeout outside {snapshot.timeout_range}")
        return ValidationResult(not reasons, tuple(reasons), None if reasons else ResourceVector(1, 1024))


_DEFAULT = DefaultCapabilityProvider()


def describe_capability(provider: str, region: str = "", deployment_mode: str = "", provider_impl: CapabilityProvider | None = None) -> CapabilitySnapshot:
    return (provider_impl or _DEFAULT).describe(provider, region, deployment_mode)


def enumerate_legal_resources(provider: str, region: str = "", deployment_mode: str = "", constraints: ResourceConstraints | None = None, provider_impl: CapabilityProvider | None = None) -> list[ResourceVector]:
    return (provider_impl or _DEFAULT).enumerate_resources(constraints or ResourceConstraints(), provider=provider, region=region, deployment_mode=deployment_mode)


def validate_candidate(candidate: CandidateSpec, provider_impl: CapabilityProvider | None = None) -> ValidationResult:
    return (provider_impl or _DEFAULT).validate(candidate)
