from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


METRICS = ("latency", "cost", "reliability", "cold_start", "state_overhead", "memory")
STATUSES = {"available", "unavailable", "partial"}


@dataclass(frozen=True)
class DeploymentCandidate:
    id: str
    provider: str
    cloud: str
    family: str
    config_path: Path
    runtime_overrides: dict[str, Any] = field(default_factory=dict)
    supported: bool = True
    unsupported_reason: str = ""
    deployment_shape: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id or not self.provider:
            raise ValueError("Candidate id and provider are required")
        if self.supported and self.family not in {"faas", "session"}:
            raise ValueError("Candidate family must be faas or session")
        if not self.supported and not self.unsupported_reason:
            raise ValueError("Unsupported candidates need a reason")
        if self.runtime_overrides:
            raise ValueError("V1 uses the resource topology from the benchmark config")


@dataclass(frozen=True)
class MetricValue:
    value: float | None = None
    status: str = "unavailable"
    reason: str = ""

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"Unknown metric status: {self.status}")
        if self.status == "unavailable" and self.value is not None:
            raise ValueError("Unavailable metrics must have value=None")
        if self.status != "unavailable" and (
            self.value is None or not math.isfinite(self.value) or self.value < 0
        ):
            raise ValueError("Measured values must be finite and nonnegative")


@dataclass
class MetricStats:
    samples: list[float] = field(default_factory=list)
    count: int = 0
    mean: float | None = None
    median: float | None = None
    min: float | None = None
    max: float | None = None
    p50: float | None = None
    p95: float | None = None
    stddev: float | None = None
    cv: float | None = None
    status: str = "unavailable"


@dataclass
class CandidateMeasurement:
    candidate: DeploymentCandidate
    successful_runs: int = 0
    failed_runs: int = 0
    latency_ms: MetricStats = field(default_factory=MetricStats)
    cost_usd: MetricStats = field(default_factory=MetricStats)
    memory_mb: MetricStats = field(default_factory=MetricStats)
    cold_start_count: MetricStats = field(default_factory=MetricStats)
    state_sync_ms: MetricStats = field(default_factory=MetricStats)
    platform_ms: MetricStats = field(default_factory=MetricStats)
    network_ms: MetricStats = field(default_factory=MetricStats)
    cold_start_rate: MetricValue = field(default_factory=MetricValue)
    failure_rate: float | None = None
    invocation_failure_rate: float | None = None
    raw_reports: list[str] = field(default_factory=list)
    run_records: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    additional_metrics: dict[str, MetricStats] = field(default_factory=dict)
    status: str = "pending"
    cleanup_status: str = "not_needed"
    exploratory: bool = False

    def __post_init__(self):
        if self.successful_runs < 0 or self.failed_runs < 0:
            raise ValueError("Run counts must be nonnegative")
        for rate in (self.failure_rate, self.invocation_failure_rate):
            if rate is not None and (not math.isfinite(rate) or not 0 <= rate <= 1):
                raise ValueError("Failure rates must be between zero and one")


@dataclass
class RecommendationConstraints:
    allowed_clouds: list[str] | None = None
    allowed_families: list[str] | None = None
    max_p95_latency_ms: float | None = None
    max_cost_usd: float | None = None
    max_failure_rate: float | None = None
    max_memory_mb: float | None = None

    def __post_init__(self):
        for name in ("max_p95_latency_ms", "max_cost_usd", "max_failure_rate", "max_memory_mb"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.max_failure_rate is not None and self.max_failure_rate > 1:
            raise ValueError("max_failure_rate must be at most one")
        if self.allowed_families is not None and not set(self.allowed_families) <= {"faas", "session"}:
            raise ValueError("Allowed families must be faas or session")


@dataclass
class CandidateScore:
    measurement: CandidateMeasurement
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    normalized_scores: dict[str, float] = field(default_factory=dict)
    effective_weights: dict[str, float] = field(default_factory=dict)
    score: float | None = None
    exclusion_reasons: list[str] = field(default_factory=list)
    on_pareto_frontier: bool = False

    @property
    def candidate(self) -> DeploymentCandidate:
        return self.measurement.candidate


@dataclass
class Recommendation:
    winner: CandidateScore | None = None
    rankings: list[CandidateScore] = field(default_factory=list)
    candidates: list[CandidateScore] = field(default_factory=list)
    pareto_frontier: list[str] = field(default_factory=list)
    objective: str = "balanced"
    weights: dict[str, float] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    confidence: str = "LOW"
    score_margin: float | None = None
    best_latency: str | None = None
    best_cost: str | None = None
    best_reliability: str | None = None
    best_balanced: str | None = None
    best_faas: str | None = None
    best_session: str | None = None
    explanation: list[str] = field(default_factory=list)
    comparisons: dict[str, Any] = field(default_factory=dict)
    benchmark: str = ""
    experiment: dict[str, Any] = field(default_factory=dict)
    pricing: dict[str, Any] = field(default_factory=dict)
    normalization_bounds: dict[str, dict[str, float]] = field(default_factory=dict)
    workload: dict[str, Any] = field(default_factory=dict)
    report_path: str = ""

# Versioned planner contracts are re-exported here so callers can use the
# historical model module as the single public import point.
from atsuite.recommender.contracts import (  # noqa: E402,F401
    SCHEMA_VERSION, BuildArtifact, CandidateSpec, DeploymentRecord, EvidenceMetric,
    ExperimentSpec, MarketDataSnapshot, TrialRecord,
)
