"""Canonical evidence gate. Legacy/imported records are readable, never upgraded."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Mapping

from .contracts import EvidenceMetric, ObjectiveSpec, ProtocolSpec
from .identity import digest
from .statistics import binomial_lower_bound, quantile_interval


@dataclass(frozen=True)
class ComparisonContext:
    scenario_id: str = ""
    workload_fingerprint: str = ""
    cost_scope: str = ""
    expected_candidate_ids: tuple[str, ...] = ()
    allow_simulated: bool = False


class EvidenceResolver:
    """Resolve content-addressed artifacts and validate ownership, not just ref text."""
    def __init__(self, catalog: Mapping[str, Mapping[str, Any]] | None = None):
        self.catalog = dict(catalog or {})

    def resolve(self, ref: str, evidence: Mapping[str, Any]) -> Mapping[str, Any]:
        item = self.catalog.get(ref)
        if item is None or ref != "sha256:" + digest(item, length=64):
            raise ValueError("unresolved_or_corrupt_reference")
        for name in ("candidate_id", "workload_fingerprint", "scenario_id"):
            if item.get(name) != evidence.get(name):
                raise ValueError("reference_ownership_mismatch")
        return item


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    blocking_reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    correctness_status: str = "unknown"
    state_validation_status: str = "unknown"
    confirmation_status: str = "unknown"
    measurement_source_status: str = "unknown"
    sample_policy_status: str = "unknown"
    configuration_status: str = "unknown"
    unit_status: str = "unknown"
    currency_status: str = "unknown"
    cost_scope_status: str = "unknown"
    coverage_status: str = "unknown"
    constraint_reasons: tuple[str, ...] = ()

    def to_dict(self):
        return asdict(self)


def required_metrics(objective: ObjectiveSpec) -> tuple[str, ...]:
    names = {"failure_rate"}
    if objective.type != "min_latency" or objective.max_cost_per_workflow is not None:
        names.add("cost")
    if objective.type != "min_cost" or objective.max_p95_workflow_ms is not None:
        names.add("p95_workflow_ms")
    if objective.max_memory_mb is not None: names.add('memory_mb')
    return tuple(sorted(names))


def evaluate_eligibility(evidence: Mapping[str, Any], objective: ObjectiveSpec,
                         protocol: ProtocolSpec | None = None,
                         context: ComparisonContext | None = None,
                         artifact_resolver: EvidenceResolver | None = None) -> EligibilityResult:
    protocol = protocol or ProtocolSpec()
    context = context or ComparisonContext()
    resolver = artifact_resolver or EvidenceResolver(evidence.get("artifacts"))
    reasons, constraints, warnings = [], [], []
    def block(code):
        if code not in reasons:
            reasons.append(code)
    if evidence.get("evidence_schema_version") != 2:
        block("legacy_unverified")
    for name in ("workload_fingerprint", "bundle_digest", "config_digest", "image_digest", "oracle_version", "scenario_id"):
        if not evidence.get(name):
            block("missing_identity:" + name)
    if context.scenario_id and context.scenario_id != evidence.get("scenario_id"):
        block("scenario_mismatch")
    if context.workload_fingerprint and context.workload_fingerprint != evidence.get("workload_fingerprint"):
        block("workload_mismatch")
    correctness = str(evidence.get("output_validation", "unknown"))
    state = str(evidence.get("state_validation", "unknown"))
    if correctness != "passed":
        block("correctness_failed" if correctness == "failed" else "correctness_unverified")
    if state != "passed" and not (state == "not_applicable" and evidence.get("state_validator") == "declared_stateless_v1"):
        block("state_validation_failed" if state == "failed" else "state_unverified")
    configuration = str(evidence.get("configuration_status", "unknown"))
    if configuration != "verified":
        block("configuration_unverified")
    if evidence.get("effective_image_digest") != evidence.get("image_digest"):
        block("image_digest_mismatch")
    try:
        configuration_artifact = resolver.resolve(evidence.get('configuration_ref',''), evidence)
        observed = configuration_artifact.get('configuration') or {}
        if configuration_artifact.get('kind') != 'configuration' or observed.get('status') != configuration or observed.get('image_digest') != evidence.get('image_digest') or observed.get('effective_image_digest') != evidence.get('effective_image_digest'):
            raise ValueError('configuration identity mismatch')
    except ValueError:
        block('configuration_reference_invalid')
    source = str(evidence.get("measurement_source", "unknown"))
    if source != "platform_observed":
        if not (source == "simulated" and context.allow_simulated):
            block("measurement_source_unverified")
        else:
            warnings.append("simulated_external_services")
    trials = list(evidence.get("trials") or ())
    confirmed = [row for row in trials if row.get("phase") == "confirmation"]
    if evidence.get("phase") != "confirmation" or not confirmed:
        block("confirmation_missing")
    if len(confirmed) != protocol.confirmation_runs or len(confirmed) < 2:
        block("confirmation_sample_policy")
    for name in ("run_id", "attempt_id"):
        ids = [row.get(name) for row in trials]
        if not all(ids) or len(set(ids)) != len(ids):
            block("duplicate_or_missing_" + name)
    for row in trials:
        if any(row.get(name) != evidence.get(name) for name in ("candidate_id", "scenario_id", "workload_fingerprint")):
            block("trial_ownership_mismatch")
        if row.get("image_digest") != evidence.get("image_digest"):
            block("trial_image_mismatch")
        if row.get("output_validation") == "failed" or row.get("state_validation") == "failed":
            block("semantic_validation_failed")
        if row.get("status") not in {"success", "failed", "timeout", "cancelled"}:
            block("attempt_incomplete")
    success = sum(row.get("status") == "success" and row.get("output_validation") == "passed" for row in confirmed)
    simultaneous_confidence = 1-(1-protocol.confidence_level)/max(1,len(context.expected_candidate_ids))
    required = required_metrics(objective)
    metrics = {}
    for name in required:
        raw = (evidence.get("metrics") or {}).get(name)
        try:
            metric = raw if isinstance(raw, EvidenceMetric) else EvidenceMetric(**dict(raw)) if raw else None
        except (ValueError, TypeError):
            block("invalid_metric:" + name)
            continue
        if metric is None or metric.value is None:
            block("missing_metric:" + name)
            continue
        metrics[name] = metric
        if metric.source != source:
            block('metric_source_mismatch:' + name)
        if metric.sample_count == 0:
            block("zero_samples:" + name)
        if metric.sample_count != len(confirmed):
            block("sample_count_mismatch:" + name)
        if metric.quality not in ({"available", "estimated"} if name == "cost" else {"available"}):
            block("metric_quality:" + name)
        if metric.measurement_kind == "predicted":
            block("predicted_metric:" + name)
        if metric.scope != "workflow":
            block("metric_scope_mismatch:" + name)
        unit = {"cost": objective.base_currency, "p95_workflow_ms": "ms", "failure_rate": "ratio",'memory_mb':'MiB'}[name]
        if metric.unit != unit:
            block("currency_mismatch" if name == "cost" else "unit_mismatch:" + name)
        if not metric.evidence_refs:
            block("missing_reference:" + name)
        resolved = []
        for ref in metric.evidence_refs:
            try:
                resolved.append(resolver.resolve(ref, evidence))
            except ValueError as exc:
                block(str(exc))
        if resolved and not any(row.get("kind") == "confirmation_trials" and row.get("trials") == confirmed for row in resolved):
            block("metric_trials_unbound:" + name)
        if name == "cost" and metric.measurement_kind == "derived":
            kinds = {row.get("kind") for row in resolved}
            if not {"usage_ledger", "rate_snapshot"} <= kinds:
                block("cost_lineage_missing")
            if evidence.get("cost_basis") != "rate_card_estimate":
                block("cost_basis_missing")
        if name == 'cost':
            import math
            bounds=(metric.ci_lower,metric.ci_upper)
            if any(v is None or not math.isfinite(v) for v in bounds):
                block('cost_uncertainty_missing')
            elif (bounds[0]>metric.value and not math.isclose(bounds[0],metric.value,rel_tol=1e-12)) or (bounds[1]<metric.value and not math.isclose(bounds[1],metric.value,rel_tol=1e-12)) or bounds[0]<0:
                block('cost_uncertainty_invalid')
        if name == "failure_rate" and not 0 <= metric.value <= 1:
            block("invalid_failure_rate")
        if name == "failure_rate" and confirmed and abs(metric.value - (1-success/len(confirmed))) > 1e-12:
            block("failure_rate_count_mismatch")
    if "cost" in required and evidence.get("cost_scope") != (context.cost_scope or objective.cost_scope):
        block("cost_scope_mismatch")
    latency = metrics.get("p95_workflow_ms")
    if latency:
        durations = [r.get('duration_ms') for r in confirmed if r.get('status')=='success' and r.get('output_validation')=='passed']
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) for v in durations):
            block('latency_observation_missing')
        else:
            interval = quantile_interval(durations, confidence_level=simultaneous_confidence)
            if abs(interval.estimate-latency.value)>1e-9:
                block('quantile_value_mismatch')
            if interval.upper is None:
                block('quantile_uncertainty_insufficient')
        blocks = [r.get('block_id') for r in confirmed]
        if not all(blocks) or len(set(blocks))!=len(blocks):
            block('confirmation_block_policy')
    if objective.max_p95_workflow_ms is not None and latency:
        if latency.value > objective.max_p95_workflow_ms:
            constraints.append("latency_slo_violation")
        within = sum(row.get("status") == "success" and row.get("output_validation") == "passed"
                     and isinstance(row.get("duration_ms"), (int, float)) and row["duration_ms"] <= objective.max_p95_workflow_ms for row in confirmed)
        if confirmed and within / len(confirmed) < .95:
            constraints.append("deadline_completion_violation")
        elif confirmed and binomial_lower_bound(within,len(confirmed),simultaneous_confidence) < .95:
            block('deadline_lower_bound_insufficient')
    if objective.min_success_rate is not None:
        lower = binomial_lower_bound(success, len(confirmed), simultaneous_confidence)
        if lower is None or lower < objective.min_success_rate:
            block("success_rate_lower_bound_insufficient")
    if objective.max_cost_per_workflow is not None and 'cost' in metrics and metrics['cost'].value > objective.max_cost_per_workflow:
        constraints.append('cost_constraint_violation')
    if objective.max_memory_mb is not None and 'memory_mb' in metrics and metrics['memory_mb'].value > objective.max_memory_mb:
        constraints.append('memory_constraint_violation')
    return EligibilityResult(not reasons and not constraints, tuple(reasons + constraints), tuple(warnings),
        correctness, state, "confirmed" if confirmed else "missing", source,
        "passed" if "confirmation_sample_policy" not in reasons else "insufficient",
        configuration, "passed" if not any("unit_mismatch" in r for r in reasons) else "failed",
        "passed" if "currency_mismatch" not in reasons else "failed",
        "passed" if "cost_scope_mismatch" not in reasons else "failed", "declared", tuple(constraints))
