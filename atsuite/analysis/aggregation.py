from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from atsuite.analysis.model import (
    SCHEMA_VERSION,
    AnalysisReport,
    CostLineItem,
    EvidenceRecord,
    InvocationObservation,
    NodeObservation,
    ProviderMetric,
    RunContext,
)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _sum_field(metrics: Iterable[ProviderMetric], key: str) -> float:
    return sum(_num(metric.fields.get(key)) for metric in metrics)


def _avg_positive(values: Iterable[float]) -> float:
    positives = [v for v in values if v >= 0 and math.isfinite(v)]
    return sum(positives) / len(positives) if positives else 0.0


class ReportAggregator:
    def aggregate(
        self,
        context: RunContext,
        *,
        nodes: Dict[int, NodeObservation],
        invocations: List[InvocationObservation],
        metrics: List[ProviderMetric],
        costs: List[CostLineItem],
        diagnostics: List[Dict[str, Any]],
        evidence: List[EvidenceRecord],
    ) -> AnalysisReport:
        diagnostics = list(diagnostics)
        def available(value):
            if value is None:
                return False
            try:
                return math.isfinite(float(value))
            except (TypeError, ValueError):
                return False

        metrics_by_call: Dict[str, List[ProviderMetric]] = defaultdict(list)
        metrics_by_node: Dict[int, List[ProviderMetric]] = defaultdict(list)
        for metric in metrics:
            if metric.invocation_call_id:
                metrics_by_call[metric.invocation_call_id].append(metric)
            if metric.node_id is not None:
                metrics_by_node[int(metric.node_id)].append(metric)

        costs_by_node: Dict[int, List[CostLineItem]] = defaultdict(list)
        for cost in costs:
            if cost.node_id is not None:
                costs_by_node[int(cost.node_id)].append(cost)

        invocation_rows = []
        for invocation in invocations:
            call_metrics = metrics_by_call.get(invocation.call_id, [])
            evidence_refs = sorted(
                {
                    ref
                    for metric in call_metrics
                    for ref in metric.evidence_refs
                }
            )
            metric_fields = call_metrics[-1].fields if call_metrics else {}
            invocation_rows.append(
                {
                    "node_id": invocation.node_id,
                    "node_name": invocation.node_name,
                    "target_id": invocation.target_id,
                    "runtime_name": invocation.runtime_name,
                    "family": invocation.family,
                    "tool_name": invocation.tool_name,
                    "call_id": invocation.call_id,
                    "provider_request_id": invocation.provider_request_id,
                    "provider_session_id": invocation.provider_session_id,
                    "status": invocation.status,
                    "error": invocation.error,
                    "client_elapsed_ms": round(invocation.client_elapsed_ms, 3),
                    "provider_duration_ms": (round(float(metric_fields["duration_ms"]), 3) if metric_fields.get("duration_ms") is not None else None),
                    "evidence_refs": evidence_refs,
                    "provider_metadata": invocation.provider_metadata,
                }
            )

        node_rows: Dict[str, Dict[str, Any]] = {}
        for node_id, node in sorted(nodes.items()):
            node_metrics = metrics_by_node.get(node_id, [])
            node_costs = costs_by_node.get(node_id, [])
            compute_ms = _sum_field(node_metrics, "tool_exec_ms")
            if not any(available(m.fields.get("tool_exec_ms")) for m in node_metrics):
                compute_ms = _sum_field(node_metrics, "duration_ms")
            initialize_ms = _sum_field(node_metrics, "init_duration_ms")
            client_e2e_ms = _sum_field(node_metrics, "client_e2e_ms")
            app_e2e_ms = _sum_field(node_metrics, "app_e2e_ms")
            tool_exec_ms = _sum_field(node_metrics, "tool_exec_ms")
            state_sync_ms = _sum_field(node_metrics, "state_sync_overhead_ms")
            framework_ms = _sum_field(node_metrics, "framework_overhead_ms")
            platform_ms = _sum_field(node_metrics, "platform_time_ms")
            network_ms = _sum_field(node_metrics, "network_time_ms")
            if platform_ms <= 0 and client_e2e_ms > 0 and app_e2e_ms > 0:
                platform_ms = max(0.0, client_e2e_ms - app_e2e_ms)
            idle_ms = max(0.0, float(node.elapsed_ms or 0.0) - compute_ms - initialize_ms)
            node_rows[str(node_id)] = {
                "node_name": node.node_name,
                "node_type": node.node_type,
                "runtime_name": node.runtime_name,
                "target_id": node.target_id,
                "family": node.family,
                "runtime_config": node.runtime_config,
                "user_e2e_ms": round(node.elapsed_ms, 3),
                "compute_time_ms": round(compute_ms, 3),
                "initialize_time_ms": round(initialize_ms, 3),
                "idle_time_ms": round(idle_ms, 3),
                "client_e2e_ms": round(client_e2e_ms, 3),
                "app_e2e_ms": round(app_e2e_ms, 3),
                "tool_exec_ms": round(tool_exec_ms, 3),
                "state_sync_overhead_ms": round(state_sync_ms, 3),
                "framework_overhead_ms": round(framework_ms, 3),
                "platform_time_ms": round(platform_ms, 3),
                "network_time_ms": round(network_ms, 3),
                "pre_tool_ms": round(_sum_field(node_metrics, "pre_tool_ms"), 3),
                "post_tool_ms": round(_sum_field(node_metrics, "post_tool_ms"), 3),
                "cold_start_count": sum(1 for m in node_metrics if _is_true(m.fields.get("is_cold_start"))),
                "avg_duration_ms": round(
                    _avg_positive(
                        _num(m.fields.get("duration_ms"), _num(m.fields.get("elapsed_time_ms")))
                        for m in node_metrics if available(m.fields.get("duration_ms")) or available(m.fields.get("elapsed_time_ms"))
                    ),
                    3,
                ),
                "avg_memory_mb": round(
                    _avg_positive(_num(m.fields.get("memory_usage_mb")) for m in node_metrics if available(m.fields.get("memory_usage_mb"))),
                    3,
                ),
                "request_ids": [m.provider_request_id for m in node_metrics if m.provider_request_id],
                "call_ids": [m.invocation_call_id for m in node_metrics if m.invocation_call_id],
                "evidence_refs": sorted({ref for metric in node_metrics for ref in metric.evidence_refs}),
                "cost": round(sum(cost.amount for cost in node_costs), 10),
                "diagnostics": list(node.diagnostics),
            }

        field_sources = {
            "compute_time_ms": ("tool_exec_ms", "duration_ms"),
            "initialize_time_ms": ("init_duration_ms",),
            "client_e2e_ms": ("client_e2e_ms",), "app_e2e_ms": ("app_e2e_ms",),
            "tool_exec_ms": ("tool_exec_ms",), "state_sync_overhead_ms": ("state_sync_overhead_ms",),
            "framework_overhead_ms": ("framework_overhead_ms",), "platform_time_ms": ("platform_time_ms",),
            "network_time_ms": ("network_time_ms",), "pre_tool_ms": ("pre_tool_ms",),
            "post_tool_ms": ("post_tool_ms",), "avg_memory_mb": ("memory_usage_mb",),
            "avg_duration_ms": ("duration_ms",), "cold_start_count": ("is_cold_start",),
        }
        for node_id, row in node_rows.items():
            calls = [call for call in invocations if call.node_id == int(node_id)]
            if not calls:
                continue
            missing = []
            for field, sources in field_sources.items():
                def observed(call):
                    return any(any((metric.fields.get(key) is not None if key == "is_cold_start" else available(metric.fields.get(key))) for key in sources)
                               for metric in metrics_by_call.get(call.call_id, []))
                if not all(observed(call) for call in calls):
                    row[field] = None
                    missing.append(field)
            if row["compute_time_ms"] is None or row["initialize_time_ms"] is None:
                row["idle_time_ms"] = None
            if not costs_by_node.get(int(node_id)):
                row["cost"] = None
            if missing:
                diagnostics.append({"code": "missing_metrics", "node_id": int(node_id), "fields": missing})

        def total(key):
            values = [row[key] for row in node_rows.values()]
            return round(sum(values), 3) if all(value is not None for value in values) else None

        memory_values = [row["avg_memory_mb"] for node_id, row in node_rows.items() if any(call.node_id == int(node_id) for call in invocations)]
        total_cost = sum(cost.amount for cost in costs)
        summary = {
            "total_nodes": len(nodes),
            "run_user_e2e_ms": round(max(0.0, (context.end_time - context.start_time) * 1000.0), 3)
            if context.start_time and context.end_time
            else None,
            "total_node_user_e2e_ms": total("user_e2e_ms"),
            "total_compute_time_ms": total("compute_time_ms"),
            "total_initialize_time_ms": total("initialize_time_ms"),
            "total_idle_time_ms": total("idle_time_ms"),
            "total_client_e2e_ms": total("client_e2e_ms"),
            "total_app_e2e_ms": total("app_e2e_ms"),
            "total_tool_exec_ms": total("tool_exec_ms"),
            "total_state_sync_overhead_ms": total("state_sync_overhead_ms"),
            "total_framework_overhead_ms": total("framework_overhead_ms"),
            "total_platform_time_ms": total("platform_time_ms"),
            "total_network_time_ms": total("network_time_ms"),
            "cold_start_total": total("cold_start_count"),
            "avg_memory_usage_mb": (round(_avg_positive(memory_values), 3) if memory_values and all(value is not None for value in memory_values) else None),
            "total_price": round(total_cost, 10) if costs else None,
            "cost_basis": "estimated_from_provider_usage" if costs else "unavailable",
            "total_cpu_price": round(sum(cost.amount for cost in costs if cost.category == "cpu"), 10) if costs else None,
            "total_memory_price": round(sum(cost.amount for cost in costs if cost.category == "memory"), 10) if costs else None,
            "storage_price": round(sum(cost.amount for cost in costs if cost.category == "storage"), 10) if costs else None,
            "currency": costs[0].currency if costs else "USD",
            "evidence_count": len(evidence),
        }
        if invocations and not all(any(cost.invocation_call_id == call.call_id for cost in costs) for call in invocations):
            summary["total_price"] = None
            diagnostics.append({"code": "incomplete_cost_coverage"})
        total_node_time = summary["total_node_user_e2e_ms"]
        summary["idle_ratio"] = None if summary["total_idle_time_ms"] is None else round(
            (summary["total_idle_time_ms"] / total_node_time) if total_node_time else 0.0,
            6,
        )

        report = AnalysisReport(
            schema_version=SCHEMA_VERSION,
            run=context.to_dict(),
            summary=summary,
            nodes=node_rows,
            invocations=invocation_rows,
            costs=[cost.to_dict() for cost in costs],
            diagnostics=list(diagnostics),
        )
        return report
