import tempfile
import unittest
from pathlib import Path

from atsuite.recommender.candidates import generate_candidates
from atsuite.recommender.experiment import ExperimentRunner
from tests.test_recommender_candidates import make_config
from tests.test_recommender_metrics import run_record


class FakeLifecycle:
    instances = []
    fail_deploy = False

    def __init__(self, candidate, output_dir):
        self.calls = []
        self.instances.append(self)

    def build(self):
        self.calls.append("build")

    def deploy(self):
        self.calls.append("deploy")
        if self.fail_deploy:
            raise SystemExit("Deployment failed")

    def run(self, uid, phase):
        self.calls.append(uid)
        if uid.endswith("run-1"):
            raise TimeoutError("Provider timeout")
        return run_record(uid=uid, phase=phase)

    def cleanup(self):
        self.calls.append("cleanup")
        return "completed"


class ExperimentTests(unittest.TestCase):
    def test_repetitions_uid_isolation_failure_and_finally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            candidate = generate_candidates(config, ["aws_lambda"])[0]
            runner = ExperimentRunner(output_dir=root / "out", lifecycle_factory=FakeLifecycle, progress=lambda _: None)
            measurement = runner.run_candidate(candidate, runs=3, warmup_runs=1, uid_prefix="recommend-abc")
            calls = FakeLifecycle.instances[-1].calls
            self.assertEqual(calls[:2], ["build", "deploy"])
            self.assertEqual(calls[-1], "cleanup")
            self.assertEqual(len(set(calls[2:-1])), 4)
            self.assertEqual((measurement.successful_runs, measurement.failed_runs), (2, 1))
            self.assertEqual(measurement.latency_ms.count, 2)
            self.assertEqual(measurement.cleanup_status, "completed")
            self.assertTrue((root / "out" / "aws_lambda" / "runs.json").is_file())

    def test_partial_deployment_failure_and_keep_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            candidate = generate_candidates(config, ["aws_lambda"])[0]
            class Failing(FakeLifecycle):
                fail_deploy = True
            runner = ExperimentRunner(output_dir=Path(tmp) / "out", lifecycle_factory=Failing, progress=lambda _: None)
            measurement = runner.run_candidate(candidate, runs=3, warmup_runs=1, uid_prefix="a")
            self.assertEqual(measurement.status, "deploy_failed")
            self.assertEqual(Failing.instances[-1].calls, ["build", "deploy", "cleanup"])
            self.assertIsNone(measurement.failure_rate)
            runner.keep_resources = True
            measurement = runner.run_candidate(candidate, runs=3, warmup_runs=1, uid_prefix="b")
            self.assertEqual(measurement.cleanup_status, "kept")
            self.assertNotIn("cleanup", Failing.instances[-1].calls)
