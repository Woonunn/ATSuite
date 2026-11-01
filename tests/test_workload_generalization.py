"""Domain-independent selection contracts; synthetic profiles are not cloud evidence."""

import copy
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path

from atsuite.routing.generalization import evaluate
from atsuite.workload import WorkloadModel, describe, family_key
from atsuite.workload.descriptor import distance, resource_distance
from tests.stage6_fixtures import tariff
from tests.test_provider_model import observation, profile


def add_profile(model, name, descriptor, *, available=100, state=1000, seconds=1):
    p = profile(state=state)
    p["request_id"] = name
    p["source"] = "synthetic"
    p["metrics"]["tool_elapsed_s"] = seconds
    model.update(p, available, descriptor=descriptor)


class ResourcePriorTests(unittest.TestCase):
    def test_unrecognized_workloads_keep_resource_conditions_and_uncertainty(self):
        for prompt, tool, extension in (
            ("Transcode this video", "encoder", ".mp4"),
            ("Retrieve relevant documents", "retriever", ".txt"),
            ("Compress this archive", "compressor", ".bin"),
        ):
            with self.subTest(tool=tool):
                model = WorkloadModel()
                small = describe(
                    prompt=prompt,
                    files=[{"name": "input" + extension, "bytes": 1000}],
                    tools=[tool],
                )
                large = describe(
                    prompt=prompt,
                    files=[{"name": "input" + extension, "bytes": 10**9}],
                    tools=[tool],
                )
                self.assertEqual(small["semantic"]["primary"], "unknown")
                for i in range(30):
                    add_profile(model, f"large-{i}", large, state=10**9, seconds=100)
                for i in range(3):
                    add_profile(model, f"small-{i}", small, state=10000, seconds=1)
                q = model.predict(101, descriptor=small)
                self.assertEqual(q["match_basis"], "resource_prior")
                self.assertTrue(q["unknown_request"])
                self.assertTrue(q["low_confidence"])
                self.assertEqual(q["eligible_success_count"], 33)
                self.assertEqual(q["sample_count"], 3)
                self.assertEqual(q["metrics"]["state_bytes"]["mean"], 10000)
                self.assertEqual(q["metrics"]["tool_elapsed_s"]["mean"], 1)
                self.assertEqual(
                    {p["request_id"] for p in q["joint_profiles"]},
                    {f"small-{i}" for i in range(3)},
                )
                # Padding and changing a filename do not change a resource prior.
                query = describe(
                    prompt=prompt + " Please be careful." * 100,
                    files=[{"name": "different" + extension, "bytes": 1000}],
                    tools=[tool],
                )
                self.assertEqual(
                    q["joint_profiles"],
                    model.predict(101, descriptor=query)["joint_profiles"],
                )

    def test_disjoint_tools_or_out_of_range_resources_do_not_fabricate_peers(self):
        model = WorkloadModel()
        d = describe(
            prompt="Compress bytes",
            files=[{"name": "input.bin", "bytes": 10**9}],
            tools=["compressor"],
        )
        add_profile(model, "large", d)
        for query in (
            describe(
                prompt="Compress bytes",
                files=[{"name": "input.bin", "bytes": 10}],
                tools=["compressor"],
            ),
            describe(
                prompt="Retrieve documents",
                files=[{"name": "input.bin", "bytes": 10**9}],
                tools=["retriever"],
            ),
        ):
            q = model.predict(101, descriptor=query)
            self.assertTrue(q["unknown_request"])
            self.assertEqual(q["sample_count"], 0)
            self.assertIsNone(q["metrics"]["state_bytes"]["mean"])

    def test_resource_prior_does_not_turn_into_a_trusted_routing_family(self):
        from tests.test_stage7 import router_fixture

        router, _ = router_fixture(epsilon=0)
        d = describe(prompt="Compress bytes", tools=["python"])
        decision = router.decide("unknown-intent", descriptor=d, as_of_ns=1000)
        self.assertEqual(decision["workload"]["match_basis"], "resource_prior")
        self.assertTrue(decision["workload"]["low_confidence"])
        self.assertEqual(decision["selected"], "known")
        self.assertEqual(decision["reason"], "validated_default")
        self.assertEqual(
            decision["provider_workload_profiles"], decision["workload"]["sample_count"]
        )

    def test_declared_shapes_and_memory_work_without_file_format_parsers(self):
        a = describe(
            prompt="Transcode image tiles",
            tools=["encoder"],
            hints={"rows": 64, "columns": 64, "memory_mb": 32},
        )
        b = describe(
            prompt="Transcode image tiles",
            tools=["encoder"],
            hints={"rows": 6400, "columns": 6400, "memory_mb": 3200},
        )
        self.assertGreater(resource_distance(a, b), 0.5)
        model = WorkloadModel()
        add_profile(model, "a", a, seconds=1)
        add_profile(model, "b", b, seconds=100)
        self.assertEqual(
            model.predict(101, descriptor=a)["metrics"]["tool_elapsed_s"]["mean"], 1
        )

    def test_unknown_size_is_not_an_empty_file(self):
        missing = describe(
            prompt="Filter documents", files=[{"name": "a.bin"}], tools=["filter"]
        )
        empty = describe(
            prompt="Filter documents",
            files=[{"name": "a.bin", "bytes": 0}],
            tools=["filter"],
        )
        self.assertGreater(resource_distance(missing, empty), 0.5)
        model = WorkloadModel()
        for i in range(3):
            add_profile(model, str(i), missing)
        self.assertTrue(model.predict(101, descriptor=missing)["low_confidence"])

    def test_no_single_primary_label_can_hide_a_new_operation(self):
        a = describe(prompt="Filter and cache documents", tools=["document_tool"])
        b = copy.deepcopy(a)
        b["semantic"]["primary"] = "stateful"
        self.assertEqual(distance(a, b), 0)
        self.assertEqual(family_key(a), family_key(b))
        simple = describe(prompt="Filter documents", tools=["document_tool"])
        self.assertGreater(distance(a, simple), 0.5)
        model = WorkloadModel()
        add_profile(model, "simple", simple)
        q = model.predict(101, descriptor=a)
        self.assertTrue(q["unknown_request"])
        self.assertEqual(q["match_basis"], "resource_prior")

    def test_other_encodings_supply_only_an_uncertain_resource_prior(self):
        a = describe(
            prompt="Filter documents",
            files=[{"name": "a.json", "bytes": 1000}],
            tools=["document_tool"],
        )
        b = describe(
            prompt="Filter documents",
            files=[{"name": "a.xml", "bytes": 1000}],
            tools=["document_tool"],
        )
        self.assertGreater(distance(a, b), 0.5)
        self.assertEqual(resource_distance(a, b), 0)
        model = WorkloadModel()
        add_profile(model, "encoded", a)
        q = model.predict(101, descriptor=b)
        self.assertEqual(q["match_basis"], "resource_prior")
        self.assertTrue(q["low_confidence"])

    def test_uniform_sample_budget_recency_future_invariance_and_snapshot(self):
        model = WorkloadModel()
        d = describe(prompt="Compress bytes", tools=["compressor"])
        for i in range(30):
            add_profile(model, str(i), d, available=100 + i, state=i + 1, seconds=i + 1)
        q = model.predict(130, descriptor=d)
        self.assertEqual(q["sample_count"], 20)
        self.assertEqual(model.predict(130)["sample_count"], 20)
        self.assertEqual(q["metrics"]["state_bytes"]["mean"], 20.5)
        self.assertEqual(
            q["metrics"]["state_bytes"]["samples"],
            q["metrics"]["tool_elapsed_s"]["samples"],
        )
        add_profile(model, "future", d, available=130, state=10**9)
        self.assertEqual(q, model.predict(130, descriptor=d))
        self.assertIn(
            "future",
            {
                p["request_id"]
                for p in model.predict(131, descriptor=d)["joint_profiles"]
            },
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.json"
            model.save(path)
            self.assertEqual(q, WorkloadModel.load(path).predict(130, descriptor=d))


class EvaluationSamplingTests(unittest.TestCase):
    def fixture(self):
        rows = []
        for i in range(40):
            row = observation(i)
            row.update(task_id="history", available_ns=5000)
            rows.append(row)
        held = observation(100)
        held["task_id"] = "held"
        rows.append(held)
        descriptors = {
            "history": describe(prompt="Retrieve documents", tools=["python"]),
            "held": describe(prompt="Transcode video", tools=["python"]),
        }
        labels = {"history": "retrieval", "held": "media"}
        return rows, descriptors, labels

    def evaluate(self, rows, descriptors, labels, **kwargs):
        return evaluate(
            rows,
            descriptors,
            {"aws_lambda": tariff()},
            held_family="media",
            family_labels=labels,
            history_limit=5,
            history_sampling="reservoir",
            sampling_seed=7,
            **kwargs,
        )

    def test_bounded_reservoir_is_causal_reproducible_and_not_first_arrivals(self):
        rows, descriptors, labels = self.fixture()
        report = self.evaluate(rows, descriptors, labels)
        prediction = report["predictions"][0]
        expected = sorted(
            range(40),
            key=lambda i: hashlib.sha256(
                json.dumps([7, "aws_lambda", str(i)]).encode()
            ).digest(),
        )[:5]
        self.assertEqual(
            {rid for _, rid in prediction["training_request_ids"]},
            set(map(str, expected)),
        )
        self.assertNotEqual(set(expected), set(range(5)))
        self.assertEqual(prediction["training_count"], 5)
        self.assertEqual(prediction["selected"], "aws_lambda")
        self.assertEqual(report["held_tasks"], ["held"])
        self.assertEqual(report["family_label_basis"], "independent")
        shuffled = copy.deepcopy(rows)
        random.Random(9).shuffle(shuffled)
        self.assertEqual(
            report["predictions"],
            self.evaluate(shuffled, descriptors, labels)["predictions"],
        )
        future = observation(200)
        future.update(task_id="history", available_ns=100000)
        future["profile"]["metrics"]["state_bytes"] = 10**12
        self.assertEqual(
            report["predictions"],
            self.evaluate(rows + [future], descriptors, labels)["predictions"],
        )
        legacy = self.evaluate(rows, descriptors, labels, descriptor_mode="legacy")
        self.assertEqual(
            prediction["training_request_ids"],
            legacy["predictions"][0]["training_request_ids"],
        )

    def test_independent_labels_must_cover_the_evaluation_data(self):
        rows, descriptors, labels = self.fixture()
        with self.assertRaisesRegex(ValueError, "family labels"):
            self.evaluate(rows, descriptors, {"held": "media"})
        shared = describe(prompt="Filter documents", tools=["python"])
        report = self.evaluate(rows, dict.fromkeys(descriptors, shared), labels)
        self.assertFalse(report["predictions"][0]["unknown_request"])
        self.assertEqual(report["held_tasks"], ["held"])

    def test_generic_cli_does_not_load_the_datasci_panel(self):
        from unittest.mock import patch
        from experiments.exp1.runner import stage7

        rows, descriptors, labels = self.fixture()
        labels["held"] = "media/video"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage7.write(root / "rows.json", rows)
            stage7.write(
                root / "config.json",
                {
                    "descriptors": descriptors,
                    "family_labels": labels,
                    "tariffs": {"aws_lambda": tariff()},
                    "history_limit": 5,
                },
            )
            with (
                patch.object(
                    stage7,
                    "prompt_panel",
                    side_effect=AssertionError("benchmark dependency"),
                ),
                patch(
                    "sys.argv",
                    [
                        "stage7",
                        "holdout",
                        "--rows",
                        str(root / "rows.json"),
                        "--config",
                        str(root / "config.json"),
                        "--output",
                        str(root / "out"),
                    ],
                ),
            ):
                stage7.main()
            report = stage7.load(root / "out/report.json")
            self.assertIsNone(report["panel"])
            self.assertEqual(len(report["reports"]), 4)
            for fold in report["reports"]:
                self.assertEqual(fold["family_label_basis"], "independent")
                self.assertEqual(fold["history_sampling"], "reservoir")
                self.assertTrue((root / "out" / fold["prediction_file"]).is_file())

    def test_observation_revision_does_not_count_as_an_extra_draw(self):
        rows, descriptors, labels = self.fixture()
        report = self.evaluate(rows, descriptors, labels)
        selected = report["predictions"][0]["training_request_ids"][0][1]
        revision = copy.deepcopy(next(r for r in rows if r["id"] == selected))
        revision["available_ns"] = 6000
        revision["measurements"]["latency_s"] = 3
        updated = self.evaluate(rows + [revision], descriptors, labels)
        self.assertEqual(
            report["predictions"][0]["training_request_ids"],
            updated["predictions"][0]["training_request_ids"],
        )
        conflicting = copy.deepcopy(revision)
        conflicting["available_ns"] = 7000
        conflicting["profile"]["metrics"]["state_bytes"] *= 2
        with self.assertRaisesRegex(ValueError, "Revision changes"):
            self.evaluate(rows + [revision, conflicting], descriptors, labels)


if __name__ == "__main__":
    unittest.main()
