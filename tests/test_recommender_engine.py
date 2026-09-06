import json
import tempfile
import unittest
from pathlib import Path

from atsuite.recommender.engine import RecommendationEngine
from atsuite.recommender.metrics import aggregate_runs
from atsuite.recommender.model import RecommendationConstraints
from atsuite.recommender.normalize import CurrencyNormalizer
from tests.test_recommender_candidates import make_config
from tests.test_recommender_metrics import run_record


class FakeExperimentRunner:
    def __init__(self, *, fail_provider=None, missing_cost=False):
        self.calls = []
        self.fail_provider = fail_provider
        self.missing_cost = missing_cost

    def run_candidate(self, candidate, *, runs, warmup_runs, uid_prefix):
        self.calls.append(candidate.id)
        if candidate.provider == self.fail_provider:
            raise RuntimeError("Provider deployment failed")
        latency, cost = (2000, .001) if candidate.family == "faas" else (1200, .002)
        records = [run_record(uid=f"{uid_prefix}-{candidate.id}-{index}", latency=latency, cost=cost) for index in range(runs)]
        if self.missing_cost:
            for record in records:
                record["summary"]["metric_availability"]["total_price"] = "unavailable"
        return aggregate_runs(candidate, records, CurrencyNormalizer())


class EngineTests(unittest.TestCase):
    def test_objectives_and_reproducible_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            runner = FakeExperimentRunner()
            engine = RecommendationEngine(runner, output_dir=Path(tmp) / "results")
            for objective, expected in (("cost", "aws_lambda"), ("latency", "aws_agentcore"), ("balanced", "aws_agentcore")):
                result = engine.recommend(config, providers=["aws_lambda", "aws_agentcore"], objective=objective)
                self.assertEqual(result.winner.candidate.id, expected)
                self.assertEqual(result.best_cost, "aws_lambda")
                self.assertEqual(result.best_latency, "aws_agentcore")
                self.assertEqual(result.best_faas, "aws_lambda")
                self.assertEqual(result.best_session, "aws_agentcore")
                payload = json.loads(Path(result.report_path).read_text())
                self.assertEqual(payload["pricing"]["normalized_currency"], "USD")
                for candidate in payload["candidates"]:
                    calculated = 0
                    for metric, weight in candidate["effective_weights"].items():
                        bound = payload["normalization_bounds"][metric]
                        value = candidate["comparison_metrics"][metric]["value"]
                        normalized = (bound["max"] - value) / (bound["max"] - bound["min"]) if bound["max"] != bound["min"] else 1
                        calculated += weight * normalized
                    self.assertAlmostEqual(calculated, candidate["score"])
                self.assertEqual(len(payload["candidates"][0]["metrics"]["latency_ms"]["samples"]), 5)
                self.assertEqual(payload["candidates"][1]["deployment_shape"]["total_cpu"], 2)
                comparison = payload["comparisons"]["runner_up"]["metrics"]["latency"]
                self.assertAlmostEqual(comparison["percent_change"], (comparison["winner"] / comparison["other"] - 1) * 100)

    def test_failures_do_not_abort_and_constraints_cannot_be_outweighed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            runner = FakeExperimentRunner(fail_provider="ali_fc")
            engine = RecommendationEngine(runner, output_dir=Path(tmp) / "results")
            result = engine.recommend(config, providers=["ali_fc", "unknown", "aws_lambda", "aws_agentcore"])
            self.assertEqual(result.winner.candidate.id, "aws_agentcore")
            self.assertEqual(len(result.candidates), 4)
            self.assertEqual(len(runner.calls), 3)
            constrained = engine.recommend(config, providers=["aws_lambda", "aws_agentcore"], objective="cost",
                                           constraints=RecommendationConstraints(max_p95_latency_ms=1500))
            self.assertIsNone(constrained.winner)
            self.assertIsNone(constrained.candidates[0].score)
            self.assertTrue(any("max_p95_latency_ms" in reason for reason in constrained.candidates[0].exclusion_reasons))
            self.assertTrue(Path(constrained.report_path).is_file())

    def test_required_vs_optional_and_validation_before_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            runner = FakeExperimentRunner(missing_cost=True)
            engine = RecommendationEngine(runner, output_dir=Path(tmp) / "results")
            result = engine.recommend(config, providers=["aws_lambda", "aws_agentcore"])
            self.assertIsNotNone(result.winner)
            self.assertEqual(result.confidence, "LOW")
            self.assertNotIn("cost", result.winner.effective_weights)
            result = engine.recommend(config, providers=["aws_lambda", "aws_agentcore"], weights={"latency": .5, "cost": .5})
            self.assertIsNone(result.winner)
            for kwargs in ({"runs": 1}, {"runs": 2}, {"weights": {"cost": .9}}, {"warmup_runs": -1}, {"fx_rates": {"USD": 2}}):
                before = len(runner.calls)
                with self.assertRaises(ValueError):
                    engine.recommend(config, **kwargs)
                self.assertEqual(len(runner.calls), before)

    def test_single_pareto_candidate_is_valid_after_two_candidates_measured(self):
        class Dominating(FakeExperimentRunner):
            def run_candidate(self, candidate, **kwargs):
                result = super().run_candidate(candidate, **kwargs)
                if candidate.family == "session":
                    from atsuite.recommender.metrics import metric_stats
                    result.cost_usd = metric_stats([.0001] * kwargs["runs"])
                return result
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            result = RecommendationEngine(Dominating(), output_dir=Path(tmp) / "results").recommend(config, providers=["aws_lambda", "aws_agentcore"])
            self.assertEqual(result.pareto_frontier, ["aws_agentcore"])
            self.assertEqual(result.winner.candidate.id, "aws_agentcore")
            self.assertEqual(len(result.rankings), 2)

    def test_raw_run_evidence_recomputes_entire_ranking(self):
        from atsuite.recommender.metrics import comparison_metrics
        from atsuite.recommender.model import CandidateScore, DeploymentCandidate
        from atsuite.recommender.scoring import score_candidates
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            result = RecommendationEngine(FakeExperimentRunner(), output_dir=Path(tmp) / "out").recommend(config)
            payload = json.loads(Path(result.report_path).read_text())
            rows = []
            for item in payload["candidates"]:
                candidate = DeploymentCandidate(item["id"], item["provider"], item["cloud"], item["family"], Path(item["config_path"]))
                measurement = aggregate_runs(candidate, item["run_records"], CurrencyNormalizer(payload["pricing"]["fx_rates"]))
                rows.append(CandidateScore(measurement, comparison_metrics(measurement, payload["experiment"]["latency_stat"])))
            ranked = score_candidates(rows, payload["objective"]["weights"])
            self.assertEqual([row.candidate.id for row in ranked], payload["rankings"])
            self.assertEqual(ranked[0].score, payload["recommendation"]["score"])

    def test_minimum_successes_excludes_unreliable_candidate(self):
        class Unreliable(FakeExperimentRunner):
            def run_candidate(self, candidate, **kwargs):
                result = super().run_candidate(candidate, **kwargs)
                if candidate.provider == "aws_lambda":
                    for record in result.run_records[2:]:
                        record["status"] = "failed"
                    result = aggregate_runs(candidate, result.run_records, CurrencyNormalizer())
                return result
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            result = RecommendationEngine(Unreliable(), output_dir=Path(tmp) / "out").recommend(config)
            bad = next(row for row in result.candidates if row.candidate.id == "aws_lambda")
            self.assertEqual(bad.measurement.failure_rate, .6)
            self.assertIsNone(bad.score)
            self.assertTrue(any("minimum" in reason for reason in bad.exclusion_reasons))
            self.assertIsNotNone(result.winner)
