"""Versioned, JSON-safe contracts for the recommender control plane.

The legacy recommender models remain in :mod:`atsuite.recommender.model`; these
contracts are the planner/execution boundary and deliberately use stdlib
 dataclasses so offline planning needs no cloud SDK or database.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1


def _finite_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        suffix = f" >= {minimum}" if minimum is not None else ""
        raise ValueError(f"{name} must be finite{suffix}")
    return number


def _strict(cls, payload: Mapping[str, Any], *, allowed: set[str] | None = None) -> dict[str, Any]:
    data = dict(payload)
    names = allowed or {item.name for item in fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ValueError(f"Unknown fields for {cls.__name__}: {', '.join(unknown)}")
    return data


def _tuple_strings(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list")
    result = tuple(str(item).strip() for item in value)
    if any(not item for item in result):
        raise ValueError(f"{name} cannot contain empty values")
    return result


@dataclass(frozen=True)
class ObjectiveSpec:
    type: str = "min_cost_under_slo"
    base_currency: str = "USD"
    cost_scope: str = "declared_runtime_scope"
    max_p95_workflow_ms: float | None = None
    min_success_rate: float | None = None
    strict_evidence: bool = True
    max_cost_per_workflow: float | None = None
    max_memory_mb: float | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ObjectiveSpec":
        data = _strict(cls, payload or {})
        objective = str(data.get("type", cls.type)).strip()
        if objective not in {"min_cost_under_slo", "min_latency", "min_cost", "balanced"}:
            raise ValueError(f"Unsupported objective: {objective}")
        currency = str(data.get("base_currency", cls.base_currency)).upper().strip()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("base_currency must be a 3-letter currency code")
        p95 = data.get("max_p95_workflow_ms")
        success = data.get("min_success_rate")
        if success is None and objective == 'min_latency':
            success = .95
        if p95 is not None:
            p95 = _finite_number(p95, "max_p95_workflow_ms", minimum=0)
        if success is not None:
            success = _finite_number(success, "min_success_rate", minimum=0)
            if success > 1:
                raise ValueError("min_success_rate must be <= 1")
        for name in ('max_cost_per_workflow','max_memory_mb'):
            if data.get(name) is not None: _finite_number(data[name], name, minimum=0)
        return cls(objective, currency, str(data.get("cost_scope", cls.cost_scope)), p95, success,
                   bool(data.get("strict_evidence", True)),data.get('max_cost_per_workflow'),data.get('max_memory_mb'))


@dataclass(frozen=True)
class BudgetSpec:
    currency: str = "USD"
    max_estimated_spend: float | None = None
    max_wall_time_seconds: int | None = None
    max_llm_tokens: int | None = None
    include_cleanup_reserve: bool = True

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "BudgetSpec":
        data = _strict(cls, payload or {})
        currency = str(data.get("currency", cls.currency)).upper().strip()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("budget currency must be a 3-letter code")
        spend = data.get("max_estimated_spend")
        if spend is not None:
            spend = _finite_number(spend, "max_estimated_spend", minimum=0)
        values = {}
        for name in ("max_wall_time_seconds", "max_llm_tokens"):
            value = data.get(name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{name} must be a nonnegative integer")
            values[name] = value
        return cls(currency, spend, values["max_wall_time_seconds"], values["max_llm_tokens"],
                   bool(data.get("include_cleanup_reserve", True)))


@dataclass(frozen=True)
class SearchSpec:
    mode: str = "adaptive"
    max_candidates: int = 48
    baseline_coverage: str = "all_eligible_provider_families"
    topology_strategies: tuple[str, ...] = ("baseline", "all_in_one", "per_node", "state_domain", "affinity_cluster", "dependency_partition")
    max_generated_topologies: int = 8
    resources_mode: str = "provider_legal_grid"
    per_target: bool = True

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "SearchSpec":
        data = _strict(cls, payload or {})
        mode = str(data.get("mode", cls.mode))
        if mode not in {"offline", "grid", "adaptive"}:
            raise ValueError("search.mode must be offline, grid, or adaptive")
        max_candidates = data.get("max_candidates", cls.max_candidates)
        max_topologies = data.get("max_generated_topologies", cls.max_generated_topologies)
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (max_candidates, max_topologies)):
            raise ValueError("search candidate limits must be positive integers")
        strategies = _tuple_strings(data.get("topology_strategies", cls.topology_strategies), "topology_strategies")
        allowed = {"baseline", "all_in_one", "per_node", "state_domain", "affinity_cluster", "dependency_partition"}
        if not set(strategies) <= allowed:
            raise ValueError("unknown topology strategy")
        return cls(mode, max_candidates, str(data.get("baseline_coverage", cls.baseline_coverage)), strategies,
                   max_topologies, str(data.get("resources_mode", cls.resources_mode)), bool(data.get("per_target", True)))


@dataclass(frozen=True)
class ProtocolSpec:
    warmup_runs: int = 2
    exploration_runs: int = 5
    confirmation_runs: int = 300
    confirmation_schedule: str = "predeclared_randomized_blocks"
    confidence_level: float = 0.95
    keep_all_failed_attempts: bool = True
    llm_replay_scale: float = 1.0

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ProtocolSpec":
        data = _strict(cls, payload or {})
        values = {name: data.get(name, getattr(cls, name)) for name in ("warmup_runs", "exploration_runs", "confirmation_runs")}
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values.values()):
            raise ValueError("protocol run counts must be nonnegative integers")
        confidence = _finite_number(data.get("confidence_level", cls.confidence_level), "confidence_level")
        if not 0 < confidence < 1:
            raise ValueError("confidence_level must be between 0 and 1")
        scale = _finite_number(data.get("llm_replay_scale", cls.llm_replay_scale), "llm_replay_scale", minimum=0)
        if data.get('confirmation_schedule',cls.confirmation_schedule)!=cls.confirmation_schedule:
            raise ValueError('only predeclared randomized confirmation blocks are supported')
        if data.get('keep_all_failed_attempts',True) is not True:
            raise ValueError('confirmation must retain every failed or timed-out attempt')
        return cls(values["warmup_runs"], values["exploration_runs"], values["confirmation_runs"],
                   str(data.get("confirmation_schedule", cls.confirmation_schedule)), confidence,
                   bool(data.get("keep_all_failed_attempts", True)), scale)


@dataclass(frozen=True)
class MarketDataPolicy:
    pricing: str = "online"
    fx_provider: str = "ecb"
    freeze_per_experiment: bool = True
    stale_policy: str = "require_explicit_acceptance"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "MarketDataPolicy":
        data = _strict(cls, payload or {})
        pricing = str(data.get("pricing", cls.pricing))
        if pricing not in {"online", "snapshot", "offline"}:
            raise ValueError("market_data.pricing must be online, snapshot, or offline")
        fx = str(data.get("fx_provider", cls.fx_provider))
        if fx not in {"ecb", "open_exchange_rates", "manual"}:
            raise ValueError("unsupported fx provider")
        stale = str(data.get("stale_policy", cls.stale_policy))
        if stale not in {"require_explicit_acceptance", "allow", "reject"}:
            raise ValueError("unsupported stale policy")
        return cls(pricing, fx, bool(data.get("freeze_per_experiment", True)), stale)


@dataclass(frozen=True)
class ExecutionSpec:
    build_workers: int = 3
    deploy_workers: int = 2
    measurement_workers_per_runner: int = 1
    isolated_namespace: bool = True
    production_apply: bool = False

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ExecutionSpec":
        data = _strict(cls, payload or {})
        values = [data.get(name, getattr(cls, name)) for name in ("build_workers", "deploy_workers", "measurement_workers_per_runner")]
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in values):
            raise ValueError("execution worker counts must be positive integers")
        return cls(*values, bool(data.get("isolated_namespace", True)), bool(data.get("production_apply", False)))


@dataclass(frozen=True)
class ExperimentSpec:
    benchmark_config: str
    providers: tuple[str, ...]
    schema_version: int = SCHEMA_VERSION
    correctness_policy: str = "require_declared_oracle"
    provider_profiles: str | None = None
    regions_by_provider: dict[str, str] = field(default_factory=dict)
    objective: ObjectiveSpec = field(default_factory=ObjectiveSpec)
    search: SearchSpec = field(default_factory=SearchSpec)
    protocol: ProtocolSpec = field(default_factory=ProtocolSpec)
    market_data: MarketDataPolicy = field(default_factory=MarketDataPolicy)
    prediction_enabled: bool = True
    prediction_fallback: str = "grid"
    advisor_enabled: bool = True
    advisor_provider: str = "openai"
    advisor_model_env: str = "ATSUITE_ADVISOR_MODEL"
    send_source_code: bool = False
    execution: ExecutionSpec = field(default_factory=ExecutionSpec)
    budget: BudgetSpec = field(default_factory=BudgetSpec)
    authorization: str = "plan_only"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExperimentSpec":
        data = _strict(cls, payload, allowed={"schema_version", "benchmark", "benchmark_config", "providers", "provider_profiles", "regions", "correctness_policy", "objective", "search", "protocol", "market_data", "prediction", "advisor", "execution", "budget", "authorization"})
        version = data.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported experiment schema_version: {version}")
        benchmark = data.get("benchmark")
        if isinstance(benchmark, Mapping):
            benchmark_data = _strict(type("Benchmark", (), {}) if False else cls, benchmark, allowed={"config", "correctness_policy"})
            config = str(benchmark_data.get("config", "")).strip()
            correctness = str(benchmark_data.get("correctness_policy", data.get("correctness_policy", cls.correctness_policy)))
        else:
            config = str(benchmark or data.get("benchmark_config", "")).strip()
            correctness = str(data.get("correctness_policy", cls.correctness_policy))
        if not config:
            raise ValueError("benchmark.config is required")
        if correctness not in {"require_declared_oracle", "allow_declared_comparator", "none"}:
            raise ValueError("unsupported correctness_policy")
        providers = _tuple_strings(data.get("providers"), "providers")
        if not providers:
            raise ValueError("providers must contain at least one provider")
        from atsuite.pipeline import PROVIDERS
        unknown_providers = sorted(set(providers) - set(PROVIDERS))
        if unknown_providers:
            raise ValueError(f"unknown providers: {', '.join(unknown_providers)}")
        regions_raw = data.get("regions") or {}
        if not isinstance(regions_raw, Mapping):
            raise ValueError("regions must be an object keyed by provider")
        unknown_regions = sorted(set(regions_raw) - set(providers))
        if unknown_regions:
            raise ValueError(f"regions contains unrequested providers: {', '.join(unknown_regions)}")
        regions = {str(provider): str(region).strip() for provider, region in regions_raw.items()}
        if any(not region for region in regions.values()):
            raise ValueError("regions cannot contain empty values")
        objective = ObjectiveSpec.from_dict(data.get("objective"))
        budget = BudgetSpec.from_dict(data.get("budget"))
        search_data = data.get("search") or {}
        protocol_data = data.get("protocol") or {}
        market_data = data.get("market_data") or {}
        prediction_data = data.get("prediction") or {}
        advisor_data = data.get("advisor") or {}
        for name, nested, allowed in (
            ("prediction", prediction_data, {"enabled", "fallback"}),
            ("advisor", advisor_data, {"enabled", "provider", "model_from_env", "send_source_code", "validate_evidence"}),
        ):
            unknown = sorted(set(nested) - allowed)
            if unknown:
                raise ValueError(f"Unknown fields for {name}: {', '.join(unknown)}")
        if budget.currency != objective.base_currency:
            raise ValueError("objective.base_currency and budget.currency must match")
        authorization = str(data.get("authorization", cls.authorization))
        if authorization not in {"plan_only", "approved_execution", "production_apply"}:
            raise ValueError("unsupported authorization")
        if authorization == "production_apply" and not bool((data.get("execution") or {}).get("production_apply", False)):
            raise ValueError("production_apply authorization requires execution.production_apply=true")
        return cls(benchmark_config=config, providers=providers, schema_version=version, correctness_policy=correctness,
                   provider_profiles=data.get("provider_profiles"), regions_by_provider=regions, objective=objective,
                   search=SearchSpec.from_dict(search_data), protocol=ProtocolSpec.from_dict(protocol_data),
                   market_data=MarketDataPolicy.from_dict(market_data), prediction_enabled=bool(prediction_data.get("enabled", True)),
                   prediction_fallback=str(prediction_data.get("fallback", "grid")), advisor_enabled=bool(advisor_data.get("enabled", True)),
                   advisor_provider=str(advisor_data.get("provider", "openai")), advisor_model_env=str(advisor_data.get("model_from_env", "ATSUITE_ADVISOR_MODEL")),
                   send_source_code=bool(advisor_data.get("send_source_code", False)), execution=ExecutionSpec.from_dict(data.get("execution")),
                   budget=budget, authorization=authorization)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "benchmark": {"config": self.benchmark_config, "correctness_policy": self.correctness_policy},
            "providers": list(self.providers),
            "provider_profiles": self.provider_profiles,
            "regions": dict(self.regions_by_provider),
            "objective": asdict(self.objective), "search": asdict(self.search), "protocol": asdict(self.protocol),
            "market_data": asdict(self.market_data),
            "prediction": {"enabled": self.prediction_enabled, "fallback": self.prediction_fallback},
            "advisor": {"enabled": self.advisor_enabled, "provider": self.advisor_provider, "model_from_env": self.advisor_model_env,
                        "send_source_code": self.send_source_code},
            "execution": asdict(self.execution), "budget": asdict(self.budget), "authorization": self.authorization,
        }


@dataclass(frozen=True)
class CandidateSpec:
    candidate_id: str
    workload_fingerprint: str
    provider: str
    family: str
    deployment_mode: str
    region: str
    runtime_architecture: str
    logical_topology: tuple[tuple[str, ...], ...]
    resources_by_target: dict[str, dict[str, Any]]
    state_policy: str = "isolated_uid"
    lifecycle_policy: str = "ephemeral"
    capability_snapshot_id: str = ""
    config_digest: str = ""

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidateSpec":
        data = _strict(cls, payload)
        topology = tuple(tuple(sorted(_tuple_strings(group, "logical_topology"))) for group in data.get("logical_topology", ()))
        resources = data.get("resources_by_target") or {}
        if not isinstance(resources, Mapping) or not resources:
            raise ValueError("resources_by_target is required")
        for target, resource in resources.items():
            if not isinstance(resource, Mapping):
                raise ValueError(f"resource vector for {target} must be an object")
            if "memory_mb" in resource:
                _finite_number(resource["memory_mb"], f"{target}.memory_mb", minimum=1)
            if "cpu" in resource:
                _finite_number(resource["cpu"], f"{target}.cpu", minimum=0.000001)
        family = str(data.get("family", ""))
        if family not in {"faas", "session"}:
            raise ValueError("family must be faas or session")
        required = ("candidate_id", "workload_fingerprint", "provider", "deployment_mode", "region", "runtime_architecture")
        if any(not str(data.get(name, "")).strip() for name in required):
            raise ValueError("candidate identity fields are required")
        return cls(str(data.get("candidate_id", "")), str(data.get("workload_fingerprint", "")), str(data.get("provider", "")), family,
                   str(data.get("deployment_mode", "")), str(data.get("region", "")), str(data.get("runtime_architecture", "")), topology,
                   {str(k): dict(v) for k, v in resources.items()}, str(data.get("state_policy", "isolated_uid")),
                   str(data.get("lifecycle_policy", "ephemeral")), str(data.get("capability_snapshot_id", "")), str(data.get("config_digest", "")))


@dataclass(frozen=True)
class BuildArtifact:
    build_id: str
    repository: str
    digest: str
    platform: str
    source_digest: str
    dockerfile_digest: str
    dependency_lock_digest: str = ""
    manifest_path: str = ""
    log_path: str = ""


@dataclass(frozen=True)
class DeploymentRecord:
    deployment_id: str
    experiment_id: str
    candidate_id: str
    physical_resources: dict[str, str]
    logical_to_physical: dict[str, str]
    requested_config: dict[str, Any]
    effective_config: dict[str, Any]
    region: str
    endpoint: str = ""
    ownership: str = ""
    created_at: str = ""
    ttl_seconds: int | None = None
    cleanup_status: str = "pending"


@dataclass(frozen=True)
class EvidenceMetric:
    name: str
    value: float | None
    unit: str
    scope: str
    sample_count: int
    measurement_kind: str = "measured"
    quality: str = "missing"
    source: str = ""
    evidence_refs: tuple[str, ...] = ()
    method_version: str = ""
    ci_lower: float | None = None
    ci_upper: float | None = None
    confidence_level: float | None = None

    def __post_init__(self):
        if self.measurement_kind not in {"measured", "derived", "predicted"}:
            raise ValueError("invalid measurement_kind")
        if self.quality not in {"available", "unsupported", "missing", "estimated", "stale"}:
            raise ValueError("invalid metric quality")
        if self.value is not None:
            _finite_number(self.value, "metric value", minimum=0)
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 0:
            raise ValueError("sample_count must be a nonnegative integer")


@dataclass(frozen=True)
class TrialRecord:
    experiment_id: str
    candidate_id: str
    scenario_id: str
    block_id: str
    run_id: str
    attempt_id: str
    status: str = "queued"
    queued_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    actual_config: dict[str, Any] = field(default_factory=dict)
    image_digest: str = ""
    business_error: str = ""
    platform_error: str = ""
    observation_error: str = ""
    output_validation: str = "unknown"
    state_validation: str = "unknown"
    measurement_window: dict[str, Any] = field(default_factory=dict)
    billing_window: dict[str, Any] = field(default_factory=dict)
    report_ref: str = ""
    evidence_ref: str = ""


@dataclass(frozen=True)
class MarketDataSnapshot:
    snapshot_id: str
    provider: str
    service: str
    sku: str
    region: str
    billing_mode: str
    unit: str
    native_currency: str
    price: str
    fetched_at: str
    observed_at: str
    source_url: str
    response_hash: str
    adapter_version: str
    freshness: str = "fresh"
    valid_from: str = ""
    valid_to: str = ""
    pricing_scope: str = "list"

    def __post_init__(self):
        if len(self.native_currency) != 3 or not self.native_currency.isalpha():
            raise ValueError("native_currency must be a 3-letter code")
        _finite_number(float(self.price), "price", minimum=0)
        if self.freshness not in {"fresh", "stale", "unknown"}:
            raise ValueError("invalid freshness")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
