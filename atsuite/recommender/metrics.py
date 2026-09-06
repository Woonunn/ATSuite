import math
import statistics

from atsuite.recommender.model import CandidateMeasurement, DeploymentCandidate, MetricStats, MetricValue
from atsuite.recommender.normalize import CurrencyNormalizer


def metric_stats(samples: list[float], *, expected_count: int | None = None, partial: bool = False) -> MetricStats:
    """Type-7 quantiles: interpolate sorted samples at (n - 1) * p; no trimming."""
    values = [float(value) for value in samples]
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Metric samples must be finite and nonnegative")
    if not values:
        return MetricStats()
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.95
    lower = math.floor(position)
    p95 = ordered[lower] + (ordered[math.ceil(position)] - ordered[lower]) * (position - lower)
    mean = statistics.mean(values)
    stddev = statistics.stdev(values) if len(values) > 1 else 0.0
    median = statistics.median(values)
    return MetricStats(
        samples=values, count=len(values), mean=mean, median=median,
        min=ordered[0], max=ordered[-1], p50=median, p95=p95,
        stddev=stddev, cv=stddev / mean if mean else 0.0,
        status="partial" if partial or (expected_count is not None and len(values) < expected_count) else "available",
    )


def summary_metric(summary: dict, key: str) -> MetricValue:
    raw = summary.get(key)
    # Legacy Analyzer defaults are not evidence of observed memory/cold starts/cost.
    status = summary.get("metric_availability", {}).get(key, "available" if key == "run_user_e2e_ms" else "unavailable")
    if raw is None or status == "unavailable":
        return MetricValue(reason=f"{key} was not observed")
    try:
        return MetricValue(float(raw), status)
    except (TypeError, ValueError):
        return MetricValue(reason=f"Invalid {key} measurement")


def aggregate_runs(candidate: DeploymentCandidate, records: list[dict], normalizer: CurrencyNormalizer) -> CandidateMeasurement:
    measured = [record for record in records if record["phase"] == "measured"]
    successful = [record for record in measured if record["status"] == "success"]
    measurement = CandidateMeasurement(
        candidate, successful_runs=len(successful), failed_runs=len(measured) - len(successful),
        failure_rate=(len(measured) - len(successful)) / len(measured) if measured else None,
        run_records=records, raw_reports=[record["report_path"] for record in records if record.get("report_path")],
        status="success" if successful else "run_failed", exploratory=len(measured) < 3,
    )
    fields = {
        "latency_ms": "run_user_e2e_ms", "cost_usd": "total_price",
        "memory_mb": "avg_memory_usage_mb", "cold_start_count": "cold_start_total",
        "state_sync_ms": "total_state_sync_overhead_ms", "platform_ms": "total_platform_time_ms",
        "network_ms": "total_network_time_ms", "compute_time_ms": "total_compute_time_ms",
        "initialize_time_ms": "total_initialize_time_ms", "framework_overhead_ms": "total_framework_overhead_ms",
        "idle_ratio": "idle_ratio",
    }
    for name, key in fields.items():
        values = []
        for record in successful:
            value = summary_metric(record.get("summary", {}), key)
            if key == "total_price" and value.value is not None:
                converted = normalizer.to_usd(value.value, record["summary"].get("currency", ""))
                value = MetricValue(converted.value, value.status if converted.value is not None else "unavailable", converted.reason)
            if value.reason:
                measurement.diagnostics.append({"kind": "metric_unavailable", "metric": name, "uid": record["uid"], "reason": value.reason})
            values.append(value)
        stats = metric_stats([value.value for value in values if value.value is not None],
                             expected_count=len(successful), partial=any(value.status == "partial" for value in values))
        if hasattr(measurement, name):
            setattr(measurement, name, stats)
        else:
            measurement.additional_metrics[name] = stats

    cold = [summary_metric(record.get("summary", {}), "cold_start_total") for record in successful]
    invocation_count = sum(record.get("summary", {}).get("invocation_count", 0) for record in successful)
    if cold and invocation_count and any(value.value is not None for value in cold):
        measurement.cold_start_rate = MetricValue(
            sum(value.value for value in cold if value.value is not None) / invocation_count,
            "available" if all(value.status == "available" for value in cold) else "partial",
        )
    all_invocations = sum(record.get("summary", {}).get("invocation_count", 0) for record in measured)
    failed_invocations = sum(record.get("summary", {}).get("failed_invocation_count", 0) for record in measured)
    measurement.invocation_failure_rate = failed_invocations / all_invocations if all_invocations else None
    measurement.diagnostics.extend(diagnostic for record in records for diagnostic in record.get("diagnostics", []))
    return measurement


def comparison_metrics(measurement: CandidateMeasurement, latency_stat: str = "p95") -> dict[str, MetricValue]:
    if latency_stat not in {"p50", "p95", "median"}:
        raise ValueError("latency_stat must be p50, p95, or median")

    def value(stats, statistic="median"):
        return MetricValue(getattr(stats, statistic), stats.status)

    return {
        "latency": value(measurement.latency_ms, latency_stat),
        "cost": value(measurement.cost_usd), "memory": value(measurement.memory_mb),
        "state_overhead": value(measurement.state_sync_ms), "cold_start": measurement.cold_start_rate,
        "reliability": MetricValue(measurement.failure_rate, "available" if measurement.failure_rate is not None else "unavailable"),
    }
