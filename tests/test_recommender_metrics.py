import math
import unittest
from pathlib import Path

from atsuite.recommender.metrics import aggregate_runs, metric_stats
from atsuite.recommender.model import DeploymentCandidate
from atsuite.recommender.normalize import CurrencyNormalizer


def run_record(uid="run", latency=100, cost=0.01, status="success", phase="measured", currency="USD"):
    summary = {
        "run_user_e2e_ms": latency, "total_price": cost, "currency": currency,
        "avg_memory_usage_mb": 100, "cold_start_total": 1, "invocation_count": 10,
        "failed_invocation_count": int(status != "success"), "total_state_sync_overhead_ms": 20,
    }
    summary["metric_availability"] = {key: "available" for key in summary}
    return {"uid": uid, "phase": phase, "status": status, "summary": summary}


class MetricsTests(unittest.TestCase):
    def test_statistics_and_outliers(self):
        stats = metric_stats([1, 2, 3, 4, 5])
        self.assertEqual((stats.count, stats.mean, stats.median, stats.p50), (5, 3, 3, 3))
        self.assertEqual(stats.p95, 4.8)
        self.assertEqual(stats.stddev, math.sqrt(2.5))
        self.assertEqual(stats.cv, math.sqrt(2.5) / 3)
        self.assertEqual(metric_stats([1, 1, 1, 1, 5000]).max, 5000)
        self.assertIsNone(metric_stats([]).p95)
        self.assertEqual(metric_stats([0]).cv, 0)
        self.assertEqual(metric_stats([7]).p95, 7)
        self.assertEqual(metric_stats([7], expected_count=3).status, "partial")
        with self.assertRaises(ValueError):
            metric_stats([float("nan")])

    def test_run_failures_warmups_missing_fx_and_partial(self):
        candidate = DeploymentCandidate("ali_fc", "ali_fc", "ali", "faas", Path("config"))
        records = [run_record(phase="warmup", latency=9000), run_record(currency="CNY"),
                   run_record(latency=200), run_record(status="failed", latency=1)]
        result = aggregate_runs(candidate, records, CurrencyNormalizer())
        self.assertEqual((result.successful_runs, result.failed_runs, result.failure_rate), (2, 1, 1/3))
        self.assertEqual(result.latency_ms.samples, [100, 200])
        self.assertEqual(result.cost_usd.status, "partial")
        self.assertEqual(result.cold_start_rate.value, 0.1)
        self.assertEqual(result.invocation_failure_rate, 1/30)
        self.assertEqual(len(result.run_records), 4)
        self.assertTrue(aggregate_runs(candidate, [run_record()], CurrencyNormalizer()).exploratory)

    def test_legacy_zero_is_unknown(self):
        candidate = DeploymentCandidate("aws_lambda", "aws_lambda", "aws", "faas", Path("config"))
        record = run_record(cost=0)
        record["summary"].pop("metric_availability")
        result = aggregate_runs(candidate, [record], CurrencyNormalizer())
        self.assertEqual(result.cost_usd.status, "unavailable")
        self.assertEqual(result.memory_mb.status, "unavailable")
