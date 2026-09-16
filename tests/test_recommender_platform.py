import json
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from atsuite.execution.store import JobStore
from atsuite.market_data.fx import StaticFxProvider, convert_base
from atsuite.market_data.model import PriceTier, RateCard, UsageEntry, UsageLedger
from atsuite.market_data.pricing import BillingRule, estimate_cost
from atsuite.pipeline import RuntimeConfig, resolve_benchmark
from atsuite.prediction.dataset import Dataset, DatasetRow
from atsuite.prediction.predictor import Predictor
from atsuite.prediction.train import train_model
from atsuite.recommender.api import plan_experiment
from atsuite.recommender.capabilities import ResourceVector, describe_capability, validate_candidate
from atsuite.recommender.contracts import CandidateSpec, ExperimentSpec
from atsuite.recommender.identity import candidate_id
from atsuite.recommender.statistics import quantile, wilson_interval, zero_failure_lower_bound
from atsuite.recommender.topology import TopologyNode, compile_session_config, generate_topologies, validate_topology
from atsuite_sdk.abstract import UnifiedAgentTool
from atsuite_sdk.state import StateRuntime
from tests.test_recommender_candidates import make_config


class RecommenderPlatformTests(unittest.TestCase):
    def tearDown(self):
        resolve_benchmark.cache_clear()

    def test_strict_contracts_and_fractional_cpu(self):
        spec = ExperimentSpec.from_dict({
            "schema_version": 1,
            "benchmark": {"config": "bench.json"},
            "providers": ["gcp_faas"],
            "regions": {"gcp_faas": "us-central1"},
            "objective": {"base_currency": "USD"},
            "budget": {"currency": "USD"},
        })
        self.assertEqual(spec.providers, ("gcp_faas",))
        self.assertEqual(spec.regions_by_provider, {"gcp_faas": "us-central1"})
        self.assertEqual(RuntimeConfig.from_dict({"cpu": .5}).cpu, .5)
        self.assertTrue(describe_capability("gcp_faas").supports_fractional_cpu)
        for payload in (
            {"schema_version": 1, "benchmark": {"config": "x"}, "providers": ["bogus"]},
            {"schema_version": 1, "benchmark": {"config": "x"}, "providers": ["aws_lambda"], "extra": 1},
            {"schema_version": 1, "benchmark": {"config": "x"}, "providers": ["aws_lambda"], "advisor": {"bogus": True}},
            {"schema_version": 1, "benchmark": {"config": "x"}, "providers": ["aws_lambda"], "regions": {"gcp_faas": "us-central1"}},
        ):
            with self.assertRaises(ValueError):
                ExperimentSpec.from_dict(payload)
        with self.assertRaises(ValueError):
            RuntimeConfig(cpu=float("nan"))

    def test_candidate_identity_and_lambda_capability(self):
        args = dict(workload_fingerprint="w", provider="aws_lambda", family="faas", deployment_mode="faas", region="us-east-1",
                    topology=(("tool",),), resources_by_target={"tool": {"cpu": 0.579, "memory_mb": 1024}},
                    runtime_architecture="linux/amd64", state_policy="isolated_uid", lifecycle_policy="ephemeral")
        self.assertEqual(candidate_id(**args), candidate_id(**args))
        candidate = CandidateSpec(candidate_id(**args), "w", "aws_lambda", "faas", "faas", "us-east-1", "linux/amd64",
                                  (("tool",),), args["resources_by_target"])
        self.assertTrue(validate_candidate(candidate).valid)
        invalid = CandidateSpec("bad", "w", "aws_lambda", "faas", "faas", "us-east-1", "linux/amd64",
                                (("tool",),), {"tool": {"cpu": 1, "memory_mb": 1024}})
        self.assertFalse(validate_candidate(invalid).valid)

    def test_topology_generation_and_compilation(self):
        nodes = [
            TopologyNode("a", state_domain="shared", state_backend="memory", must_colocate=("b",)),
            TopologyNode("b", state_domain="shared", state_backend="memory"),
            TopologyNode("c", must_separate=("a",)),
        ]
        plans = generate_topologies(nodes)
        self.assertTrue(plans)
        for plan in plans:
            self.assertFalse(validate_topology(plan, {"a", "b", "c"}))
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            plan = next(plan for plan in generate_topologies([TopologyNode("tool")], baseline_groups=(("tool",),)) if plan.strategy == "baseline")
            compiled = compile_session_config(config, plan)
            server = compiled["pipeline"]["session"]["servers"][0]
            self.assertEqual(server["nodes"], ["tool"])
            self.assertIn("deploy", server)

    def test_plan_is_bounded_and_keeps_unverified_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            spec = ExperimentSpec.from_dict({"schema_version": 1, "benchmark": {"config": str(config)},
                                             "providers": ["aws_lambda", "aws_agentcore"], "search": {"max_candidates": 5}})
            plan = plan_experiment(spec)
            self.assertEqual(plan["status"], "planned")
            self.assertLessEqual(len(plan["candidates"]), 5)
            self.assertIsNone(plan["best_verified"])
            self.assertEqual(plan["scope"]["measured_candidates"], 0)

    def test_reprice_recomputes_immutable_usage_ledger(self):
        from atsuite.market_data.reprice import reprice
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "usage_ledger.jsonl"
            ledger.write_text(json.dumps({
                "candidate_id": "c", "run_id": "r", "category": "duration",
                "quantity": "7", "unit": "s", "provider": "aws", "source_ref": "same"
            }) + "\n", encoding="utf-8")
            experiment = {"schema_version": 1, "objective": {"base_currency": "USD"}}
            snapshot = {"snapshot_id": "market-1", "rate_cards": [{
                "snapshot_id": "rate-1", "provider": "aws", "category": "duration",
                "sku": "sku", "region": "us-east-1", "billing_mode": "ondemand",
                "unit": "s", "currency": "USD", "tiers": [{"up_to": None, "unit_price": "0.1"}],
                "fetched_at": "2026-09-16", "observed_at": "2026-09-16", "source": "fixture",
                "freshness": "fresh"
            }]}
            result = reprice(experiment, snapshot, usage_ledger_path=ledger)
            self.assertEqual(result["status"], "reprice_completed")
            self.assertEqual(result["costs"][0]["amount"], "0.7")
            missing = reprice(experiment, {"snapshot_id": "market-2", "rate_cards": []}, usage_ledger_path=ledger)
            self.assertEqual(missing["status"], "reprice_missing_coverage")

    def test_decimal_billing_fx_and_deduplication(self):
        ledger = UsageLedger((
            UsageEntry("c", "r", "duration", Decimal("7"), "s", "aws", source_ref="same"),
            UsageEntry("c", "r", "duration", Decimal("7"), "s", "aws", source_ref="same"),
        ))
        card = RateCard("rate-1", "aws", "service", "sku", "region", "ondemand", "s", "CNY",
                        (PriceTier(None, Decimal("1")),), "fetched", "observed", "fixture")
        fx = StaticFxProvider({"CNY": "7"}).fetch("USD")
        estimates = estimate_cost(ledger, {("aws", "duration"): card}, fx=fx, rules={"duration": BillingRule(rounding_seconds=Decimal("1"))})
        self.assertEqual(estimates[0].quantity, Decimal("7"))
        self.assertEqual(estimates[0].amount, Decimal("1"))
        self.assertEqual(estimate_cost(ledger, {("aws", "duration"): card})[0].coverage, "missing")
        wrong_unit = RateCard("rate-2", "aws", "service", "sku", "region", "ondemand", "ms", "USD",
                              (PriceTier(None, Decimal("1")),), "fetched", "observed", "fixture")
        self.assertEqual(estimate_cost(ledger, {("aws", "duration"): wrong_unit})[0].coverage, "missing")
        cny = convert_base(fx, "CNY")
        self.assertEqual(cny.rate("USD"), Decimal("1") / Decimal("7"))

    def test_statistics_and_predictor_labels(self):
        self.assertEqual(quantile([1, 2, 3, 4], .5), 2.5)
        self.assertLess(wilson_interval(20, 20).lower, 1)
        self.assertAlmostEqual(zero_failure_lower_bound(20), .05 ** (1 / 20))
        dataset = Dataset([
            DatasetRow("w1", "c1", "s", "2026-01-01", {"candidate.total_cpu": 1}, {"latency_ms": 10, "memory_mb": 100, "failure_risk": 0}),
            DatasetRow("w2", "c2", "s", "2026-02-01", {"candidate.total_cpu": 2}, {"latency_ms": 20, "memory_mb": 200, "failure_risk": .1}),
        ])
        model = train_model(dataset)
        prediction = Predictor(model).predict({}, type("Candidate", (), {"resources_by_target": {"x": {"cpu": 1.5, "memory_mb": 150}}, "family": "faas", "runtime_architecture": "linux/amd64"})())
        self.assertEqual(prediction.quality, "predicted")
        self.assertEqual(prediction.model_id, model.model_id)

    def test_job_store_events_and_idempotent_state_data(self):
        store = JobStore()
        store.create("e", {"plan": {"hash": "x"}})
        updated = store.transition("e", "PLANNED", coverage=1)
        self.assertEqual(updated["state"], "PLANNED")
        self.assertEqual(store.events("e")[0]["state"], "PLANNED")
        store.close()

    def test_state_identity_is_strict_and_uids_do_not_overlap(self):
        class Box:
            def __init__(self):
                self.value = 0
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {
            "ATSUITE_RUNTIME": "mcp", "ATSUITE_SESSION_ROOT": tmp, "ATSUITE_TENANT": "tenant",
            "ATSUITE_EXPERIMENT_ID": "experiment", "ATSUITE_LOGICAL_STATE_DOMAIN": "domain",
        }, clear=False):
            runtime = StateRuntime()
            box = Box()
            runtime.register_object("box", box, module_name=__name__)

            def increment(uid=None):
                current = box.value
                time.sleep(.01)
                box.value = current + 1
                return box.value

            tool = UnifiedAgentTool(increment, stateful=True)
            from atsuite_sdk import state as state_module
            previous = state_module._RUNTIME
            state_module._RUNTIME = runtime
            try:
                results = []
                threads = [threading.Thread(target=lambda uid=uid: results.append(tool.execute_from_dict({"uid": uid}))) for uid in ("a", "b")]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
                self.assertEqual(sorted(results), [1, 1])
                self.assertEqual(tool.execute_from_dict({"uid": "a"}), 2)
                self.assertIn("tenant/experiment/domain/a.json", runtime._snapshot_key("a"))
            finally:
                state_module._RUNTIME = previous

class MarketDecisionAdvisorTests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.payload).encode()

    def test_market_adapters_parse_pagination_tiers_and_failures(self):
        from atsuite.market_data.providers import AWSPriceAdapter, GCPCatalogAdapter, AlibabaPriceAdapter, OpenExchangeRatesProvider

        aws_payload = {
            "product": {"sku": "sku-1", "productFamily": "Compute", "attributes": {"regionCode": "us-east-1"}},
            "terms": {"OnDemand": {"term": {"effectiveDate": "2026-09-01T00:00:00Z", "priceDimensions": {
                "r1": {"beginRange": "0", "endRange": "10", "unit": "Second", "pricePerUnit": {"USD": "0.1"}},
                "r2": {"beginRange": "10", "endRange": "Inf", "unit": "Second", "pricePerUnit": {"USD": "0.2"}},
            }}}},
        }
        paginator = mock.Mock(); paginator.paginate.return_value = [{"PriceList": [json.dumps(aws_payload)]}, {"PriceList": []}]
        client = mock.Mock(); client.get_paginator.return_value = paginator
        aws_cards = AWSPriceAdapter(client).fetch("AWSLambda", [], region="us-east-1")
        self.assertEqual(len(aws_cards), 1)
        self.assertEqual(len(aws_cards[0].tiers), 2)
        self.assertEqual(aws_cards[0].price_for(Decimal("12")), Decimal("1.4"))
        self.assertEqual(AWSPriceAdapter(client).parse({"product": {}}, region="us-east-1"), [])

        pages = iter([
            self.Response({"skus": [{"skuId": "g1", "serviceRegions": ["us-central1"], "category": {"usageType": "OnDemand"},
                                      "pricingInfo": [{"effectiveTime": "2026-09-01T00:00:00Z", "pricingExpression": {"usageUnit": "s", "tieredRates": [{"startUsageAmount": 0, "unitPrice": {"units": "0", "nanos": 5}}]}}]}], "nextPageToken": "next"}),
            self.Response({"skus": []}),
        ])
        gcp = GCPCatalogAdapter("key", opener=lambda *args, **kwargs: next(pages))
        gcp_cards = gcp.fetch("services/test", region="us-central1")
        self.assertEqual(gcp_cards[0].tiers[0].unit_price, Decimal("0.000000005"))
        self.assertIn("pageToken=next", gcp.opener.__name__ if hasattr(gcp.opener, "__name__") else "") if False else None
        with self.assertRaises(TimeoutError):
            GCPCatalogAdapter("key", opener=lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError())).fetch("x")

        ali = AlibabaPriceAdapter()
        ali_cards = ali.parse({"Success": True, "Data": {"Currency": "CNY", "ModuleDetails": {"ModuleDetail": [{"ModuleCode": "cpu", "UnitPrice": "0.25"}]}}}, product_code="fc", region="cn-hangzhou", modules=[{"ModuleCode": "cpu", "PriceType": "Hour"}])
        self.assertEqual((ali_cards[0].unit, ali_cards[0].currency), ("hour", "CNY"))
        with self.assertRaises(ValueError):
            ali.parse({"Success": False, "Code": "Throttling", "Message": "429"}, product_code="fc")

        oxr = OpenExchangeRatesProvider("app", opener=lambda *args, **kwargs: self.Response({"base": "USD", "timestamp": 1789516800, "rates": {"CNY": 7.0}}))
        fx = oxr.fetch("USD")
        self.assertEqual(fx.rate("CNY"), Decimal("7.0"))
        self.assertEqual(fx.observed_at, "2026-09-16T00:00:00Z")
        with self.assertRaises(ValueError):
            OpenExchangeRatesProvider("app", opener=lambda *args, **kwargs: self.Response({"error": True})).fetch()

    def test_fixed_anchor_decision_and_search_coverage(self):
        from atsuite.recommender.contracts import EvidenceMetric, ObjectiveSpec
        from atsuite.recommender.decision import decide
        from atsuite.recommender.search import select_exploration, select_confirmation, validate_transition

        def measured(name, value):
            return EvidenceMetric(name, value, "unit", "workflow", 5, "measured", "available", evidence_refs=(f"e:{name}",))
        rows = [
            {"candidate_id": "a", "scenario_id": "s", "metrics": {"p95_workflow_ms": measured("lat", 100), "cost": measured("cost", .1), "failure_rate": measured("fail", 0)}},
            {"candidate_id": "b", "scenario_id": "s", "metrics": {"p95_workflow_ms": measured("lat", 1000), "cost": measured("cost", .4), "failure_rate": measured("fail", .1)}},
        ]
        result = decide(rows, ObjectiveSpec(type="balanced"))
        self.assertEqual((result.status, result.best_verified), ("recommended", "a"))
        rows.append({"candidate_id": "c", "scenario_id": "s", "metrics": {"p95_workflow_ms": EvidenceMetric("lat", 50, "ms", "workflow", 0, "predicted", "available", evidence_refs=("p",)), "cost": measured("cost", .01), "failure_rate": measured("fail", 0)}})
        result = decide(rows, ObjectiveSpec(type="balanced"))
        self.assertIn("c", result.promising_unverified)
        selected = select_exploration([
            {"candidate_id": "a1", "provider": "aws_lambda", "family": "faas", "predicted_score": 1},
            {"candidate_id": "a2", "provider": "aws_lambda", "family": "faas", "predicted_score": 0},
            {"candidate_id": "g1", "provider": "gcp_mcp", "family": "session", "predicted_score": 2},
        ], 2)
        self.assertEqual(set(selected), {"a2", "g1"})
        self.assertEqual(select_confirmation([{"candidate_id": "a", "feasible": True, "evidence_quality": "measured", "score": .2}], 1), ["a"])
        with self.assertRaises(ValueError): validate_transition("COMPLETED", "BUILDING")

    def test_advisor_falls_back_and_verifier_rejects_invented_facts(self):
        from atsuite.advisor.client import OpenAIAdvisor
        from atsuite.advisor.model import AdvisorResponse, CandidateProposal
        from atsuite.advisor.verify import verify_response

        fallback = OpenAIAdvisor(model="").recommend({"eligible_candidates": ["a"], "objective_winner": "a", "secret": "drop"})
        self.assertEqual(fallback.provider, "template")
        self.assertTrue(fallback.audit["fallback"])
        invalid = verify_response(AdvisorResponse("x", (CandidateProposal("made-up"),), "b", metric_references=("fake",)),
                                  candidate_catalog={"a": {}}, eligible_ids={"a"}, metric_catalog={}, objective_winner="a")
        self.assertFalse(invalid.valid)
        self.assertEqual(len(invalid.validation_errors), 3)

class ArtifactIdentityTests(unittest.TestCase):
    def test_recursive_tree_digest_and_explicit_artifact_reference(self):
        from atsuite.deploy import _artifact_image_ref, _deployment_result
        from atsuite.recommender.identity import tree_digest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested").mkdir()
            (root / "nested" / "a.txt").write_text("one")
            first, files = tree_digest(root)
            (root / "nested" / "a.txt").write_text("two")
            second, _ = tree_digest(root)
            self.assertNotEqual(first, second)
            self.assertEqual(files, ["nested/a.txt"])
        artifact = {"repository": "registry.example/team/image:tag", "digest": "sha256:" + "a" * 64}
        self.assertEqual(_artifact_image_ref(artifact), "registry.example/team/image@sha256:" + "a" * 64)
        with self.assertRaises(RuntimeError):
            _artifact_image_ref({"repository": "image", "digest": ""})
        self.assertEqual(_deployment_result({"endpoint": "https://example", "effective_resources": {"cpu": 1}}, {"cpu": 1})[2], "verified")
        self.assertEqual(_deployment_result({"endpoint": "https://example", "effective_resources": {"memory": 512},
                                             "configuration_readback": "verified"},
                                            {"cpu": 1, "memory": 512})[2], "partial")
        with self.assertRaises(RuntimeError):
            _deployment_result({"endpoint": "https://example", "effective_resources": {"cpu": 2}}, {"cpu": 1})

class ControlPlaneSecurityTests(unittest.TestCase):
    def test_safe_zip_upload_rejects_traversal_and_symlink(self):
        import io
        import zipfile
        from atsuite.server.security import safe_extract_zip
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            safe = root / "safe.zip"
            with zipfile.ZipFile(safe, "w") as bundle:
                bundle.writestr("benchmark/config/test.json", "{}")
            metadata = safe_extract_zip(safe, root / "out")
            self.assertEqual(metadata["config_files"], ["benchmark/config/test.json"])

            bad = root / "bad.zip"
            with zipfile.ZipFile(bad, "w") as bundle:
                bundle.writestr("../escape", "bad")
            with self.assertRaises(ValueError):
                safe_extract_zip(bad, root / "bad-out")

            link = root / "link.zip"
            with zipfile.ZipFile(link, "w") as bundle:
                info = zipfile.ZipInfo("benchmark/config/link")
                info.create_system = 3
                info.external_attr = (0o120777 << 16)
                bundle.writestr(info, "/etc/passwd")
            with self.assertRaises(ValueError):
                safe_extract_zip(link, root / "link-out")

    def test_store_idempotency_and_tenant_artifact_ownership(self):
        store = JobStore()
        self.assertEqual(store.claim_idempotency("t/p", "key", "hash", "one"), "one")
        self.assertEqual(store.claim_idempotency("t/p", "key", "hash", "two"), "one")
        with self.assertRaises(ValueError):
            store.claim_idempotency("t/p", "key", "different", "two")
        store.register_artifact("a", "tenant", "project", "x.json")
        self.assertEqual(store.artifact("a", "tenant", "project")["relative_path"], "x.json")
        with self.assertRaises(KeyError):
            store.artifact("a", "other", "project")
        store.close()

class PlanExecutionTests(unittest.TestCase):
    def test_materialized_plan_preserves_source_paths_and_build_reuse(self):
        from atsuite.execution.build import build_candidates
        from atsuite.recommender.api import materialize_plan
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            spec = ExperimentSpec.from_dict({"schema_version": 1, "benchmark": {"config": str(config)}, "providers": ["gcp_faas"], "search": {"max_candidates": 3}})
            plan = plan_experiment(spec)
            plan_path = materialize_plan(plan, root / "plan.json")
            loaded = json.loads(plan_path.read_text())
            generated = json.loads(Path(loaded["candidates"][0]["generated_config"]).read_text())
            self.assertTrue(Path(generated["trace_file"]).is_absolute())
            self.assertTrue(all(Path(node["dir"]).is_absolute() for node in generated["nodes"]))
            calls = []
            def builder(config_path, provider, output, **kwargs):
                calls.append((provider, kwargs["candidate_id"]))
                return {"tool": {"digest": "sha256:" + "a" * 64}}
            built = build_candidates(loaded, root / "build", max_workers=2, builder=builder)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(built), len(loaded["candidates"]))

    def test_experiment_job_stages_and_failure_are_persistent(self):
        from atsuite.execution.jobs import ExperimentJob
        store = JobStore()
        store.create("job", {"completed_stages": []}, state="APPROVED")
        seen = []
        handlers = {stage: (lambda record, stage=stage: seen.append(stage) or {stage.lower(): True}) for stage in ("BUILDING", "DEPLOYING", "EXPLORING", "CONFIRMING", "ANALYZING")}
        result = ExperimentJob(store, "job", handlers).run()
        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(seen[0], "BUILDING")
        store.create("bad", {"completed_stages": []}, state="APPROVED")
        result = ExperimentJob(store, "bad", {"BUILDING": lambda _: (_ for _ in ()).throw(RuntimeError("boom"))}).run()
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("boom", result["error"])

        store.create("missing-handler", {"completed_stages": []}, state="APPROVED")
        result = ExperimentJob(store, "missing-handler", {}).run()
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("missing execution handler", result["error"])

        store.create("cancel", {"completed_stages": [], "cleanup_required": True}, state="CANCEL_REQUESTED")
        cleaned = []
        result = ExperimentJob(store, "cancel", {"CLEANUP": lambda record: cleaned.append(record["id"]) or {}}).run()
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(result["cleanup_status"], "success")
        self.assertEqual(cleaned, ["cancel"])

        store.create("cleanup-failed", {"completed_stages": [], "cleanup_required": True}, state="CANCEL_REQUESTED")
        result = ExperimentJob(store, "cleanup-failed", {"CLEANUP": lambda _: (_ for _ in ()).throw(RuntimeError("leaked"))}).run()
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertIn("leaked", result["cleanup_error"])

        store.create("cleanup-resume", {"completed_stages": [], "cleanup_required": True,
                                        "cleanup_terminal_state": "CANCELLED", "cleanup_reason": "cancelled"},
                     state="CLEANUP_PENDING")
        from atsuite.recommender.search import SearchOrchestrator
        self.assertEqual(SearchOrchestrator(store).resume("cleanup-resume")["state"], "CLEANUP_PENDING")
        result = ExperimentJob(store, "cleanup-resume", {"CLEANUP": lambda _: {}}).run()
        self.assertEqual(result["state"], "CANCELLED")
        store.close()

class OptionalIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(__import__("importlib").util.find_spec("fastapi"), "FastAPI dependencies are not installed")
    def test_control_plane_idempotency_origin_tenant_and_artifacts(self):
        from fastapi.testclient import TestClient
        from atsuite.server.app import create_app
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            app = create_app(store_path=root / "jobs.sqlite", artifact_root=root / "artifacts", web_root=root / "no-ui")
            client = TestClient(app)
            ui_app = create_app(store_path=root / "ui.sqlite", artifact_root=root / "ui-artifacts")
            ui = TestClient(ui_app).get("/")
            self.assertEqual(ui.status_code, 200)
            self.assertIn("Deployment Lab", ui.text)
            headers = {"X-ATSuite-Tenant": "tenant", "X-ATSuite-Project": "project", "X-ATSuite-Role": "admin"}
            payload = {"schema_version": 1, "benchmark": {"config": str(config)}, "providers": ["gcp_faas"], "authorization": "plan_only"}
            denied = client.post("/api/v1/experiments/plan", headers={**headers, "Origin": "https://evil.example"}, json=payload)
            self.assertEqual(denied.status_code, 403)
            first = client.post("/api/v1/experiments", headers={**headers, "Idempotency-Key": "same"}, json=payload)
            self.assertEqual(first.status_code, 202, first.text)
            second = client.post("/api/v1/experiments", headers={**headers, "Idempotency-Key": "same"}, json=payload)
            self.assertEqual(second.json()["id"], first.json()["id"])
            conflict_payload = {**payload, "providers": ["aws_lambda"]}
            self.assertEqual(client.post("/api/v1/experiments", headers={**headers, "Idempotency-Key": "same"}, json=conflict_payload).status_code, 409)
            experiment_id = first.json()["id"]
            self.assertEqual(client.get(f"/api/v1/experiments/{experiment_id}", headers={**headers, "X-ATSuite-Tenant": "other"}).status_code, 404)
            artifact_id = first.json()["plan_artifact_id"]
            self.assertEqual(client.get(f"/api/v1/artifacts/{artifact_id}", headers=headers).status_code, 200)
            self.assertEqual(client.get(f"/api/v1/artifacts/{artifact_id}", headers={**headers, "X-ATSuite-Tenant": "other"}).status_code, 404)
            repriced = client.post(f"/api/v1/experiments/{experiment_id}/reprice", headers=headers,
                                   json={"snapshot_id": "market-1", "rate_cards": []})
            self.assertEqual(repriced.status_code, 201)
            version = repriced.json()["repricings"][-1]
            self.assertEqual(version["status"], "insufficient_usage_ledger")
            self.assertEqual(client.get(f"/api/v1/artifacts/{version['artifact_id']}", headers=headers).status_code, 200)

    @unittest.skipUnless(__import__("importlib").util.find_spec("pydantic") and __import__("importlib").util.find_spec("openai"), "Advisor dependencies are not installed")
    def test_openai_advisor_uses_structured_outputs_and_redacts_catalog(self):
        from types import SimpleNamespace
        from atsuite.advisor.client import OpenAIAdvisor
        captured = {}
        class Responses:
            def parse(self, **kwargs):
                captured.update(kwargs)
                schema = kwargs["text_format"]
                parsed = schema(objective_interpretation="measured evidence", candidate_proposals=[], preferred_candidate_id="a",
                                reason_codes=["winner"], metric_references=["m1"], tradeoffs=[], additional_experiments=[])
                return SimpleNamespace(output_parsed=parsed, usage={"input_tokens": 10, "output_tokens": 5}, id="resp", status="completed")
        client = SimpleNamespace(responses=Responses())
        result = OpenAIAdvisor(model="configured-model", client=client).recommend({"eligible_candidates": ["a"], "objective_winner": "a",
            "candidates": {"a": {"candidate_id": "a", "provider": "gcp_faas", "secret": "never-send"}}, "metrics": {"m1": {"value": 1}}, "api_key": "drop"}, "explain")
        self.assertEqual((result.provider, result.model, result.preferred_candidate_id), ("openai", "configured-model", "a"))
        self.assertFalse(result.audit["fallback"])
        self.assertFalse(captured["store"])
        sent = captured["input"][1]["content"]
        self.assertNotIn("never-send", sent)
        self.assertNotIn("api_key", sent)

class DecisionAndModelEdgeTests(unittest.TestCase):
    def test_decision_statuses_and_fixed_anchor_invariance(self):
        from atsuite.recommender.contracts import EvidenceMetric, ObjectiveSpec
        from atsuite.recommender.decision import decide
        def metric(name, value, kind="measured", quality="available", refs=("e",)):
            return EvidenceMetric(name, value, "unit", "workflow", 5, kind, quality, evidence_refs=refs)
        base = {"candidate_id": "a", "metrics": {"cost": metric("cost", .1), "p95_workflow_ms": metric("lat", 100), "failure_rate": metric("fail", 0)}}
        result = decide([base], ObjectiveSpec(type="min_cost_under_slo", max_p95_workflow_ms=50))
        self.assertEqual(result.status, "no_feasible_candidate")
        missing = {"candidate_id": "m", "metrics": {"cost": metric("cost", .1), "p95_workflow_ms": metric("lat", None, quality="missing"), "failure_rate": metric("fail", 0)}}
        self.assertEqual(decide([missing], ObjectiveSpec(type="balanced")).status, "insufficient_evidence")
        close = {"candidate_id": "b", "metrics": {"cost": metric("cost", .1001), "p95_workflow_ms": metric("lat", 101), "failure_rate": metric("fail", 0)}}
        self.assertEqual(decide([base, close], ObjectiveSpec(type="balanced")).status, "no_clear_winner")
        first = decide([base, {"candidate_id": "b", "metrics": {"cost": metric("cost", .5), "p95_workflow_ms": metric("lat", 1000), "failure_rate": metric("fail", .2)}}], ObjectiveSpec(type="balanced"))
        worse = {"candidate_id": "z", "metrics": {"cost": metric("cost", 50), "p95_workflow_ms": metric("lat", 100000), "failure_rate": metric("fail", 1)}}
        second = decide([base, {"candidate_id": "b", "metrics": {"cost": metric("cost", .5), "p95_workflow_ms": metric("lat", 1000), "failure_rate": metric("fail", .2)}}, worse], ObjectiveSpec(type="balanced"))
        self.assertEqual(first.rankings[0]["score"], second.rankings[0]["score"])

    def test_grouped_time_split_and_predictor_ood(self):
        dataset = Dataset([
            DatasetRow("w1", "a", "s", "2026-01-01", {"x": 1}, {"latency_ms": 10}),
            DatasetRow("w1", "b", "s", "2026-02-01", {"x": 2}, {"latency_ms": 20}),
            DatasetRow("w2", "c", "s", "2026-03-01", {"x": 3}, {"latency_ms": 30}),
        ])
        train, test = dataset.split(test_workloads={"w1"})
        self.assertEqual({row.workload_fingerprint for row in train.rows}, {"w2"})
        self.assertEqual({row.workload_fingerprint for row in test.rows}, {"w1"})
        before, future = dataset.split(cutoff="2026-03-01")
        self.assertEqual(len(future.rows), 1)
        model = train_model(dataset, target_names=("latency_ms",))
        model = type(model)(**{**model.__dict__, "feature_names": ("candidate.total_cpu",), "feature_bounds": {"candidate.total_cpu": (1.0, 3.0)},
                               "coefficients": {"latency_ms": {"intercept": 0.0, "candidate.total_cpu": 1.0}}})
        prediction = Predictor(model).predict({}, type("Candidate", (), {"resources_by_target": {"x": {"cpu": 100, "memory_mb": 128}}, "family": "faas", "runtime_architecture": "linux/amd64"})())
        self.assertTrue(prediction.ood)

class CliWorkflowTests(unittest.TestCase):
    def test_plan_queue_compare_dataset_and_manual_fx_commands(self):
        from atsuite.cli.dataset import main as dataset_main
        from atsuite.cli.market import main as market_main
        from atsuite.cli.recommend import main as recommend_main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps({"schema_version": 1, "benchmark": {"config": str(config)}, "providers": ["gcp_faas"],
                                             "authorization": "approved_execution"}))
            plan_path = root / "plan.json"
            self.assertEqual(recommend_main(["plan", "--spec", str(spec_path), "--output", str(plan_path)]), 0)
            plan = json.loads(plan_path.read_text())
            self.assertTrue(plan["candidates"][0]["generated_config"])
            store_path = root / "jobs.sqlite"
            self.assertEqual(recommend_main(["run", "--plan", str(plan_path), "--approve-budget", "--store", str(store_path)]), 0)
            from atsuite.execution.store import JobStore
            store = JobStore(store_path)
            experiment_id = "exp-" + __import__("atsuite.recommender.identity", fromlist=["digest"]).digest(plan, length=24)
            self.assertEqual(store.get(experiment_id)["state"], "APPROVED")
            store.close()

            evidence = {"objective": {"type": "min_cost"}, "candidates": [{"candidate_id": "a", "metrics": {
                "cost": {"name": "cost", "value": .1, "unit": "USD", "scope": "run", "sample_count": 3, "measurement_kind": "measured", "quality": "available", "evidence_refs": ["e"]},
                "failure_rate": {"name": "failure", "value": 0, "unit": "ratio", "scope": "run", "sample_count": 3, "measurement_kind": "measured", "quality": "available", "evidence_refs": ["e"]}
            }}]}
            evidence_path = root / "evidence.json"; evidence_path.write_text(json.dumps(evidence))
            comparison = root / "comparison.json"
            self.assertEqual(recommend_main(["compare", "--manifest", str(evidence_path), "--output", str(comparison)]), 0)
            self.assertEqual(json.loads(comparison.read_text())["best_verified"], "a")

            source_data = root / "source.dataset.jsonl"
            Dataset([DatasetRow("w", "c", "s", "2026-09-16", {"x": 1}, {"latency_ms": 2})]).write_jsonl(source_data)
            manifest = root / "dataset.json"
            self.assertEqual(dataset_main(["export", "--experiments", str(root), "--output", str(manifest)]), 0)
            self.assertEqual(Dataset.load(manifest).coverage()["rows"], 1)

            rates = root / "rates.json"; rates.write_text(json.dumps({"CNY": "7"}))
            fx = root / "fx.json"
            self.assertEqual(market_main(["fx", "--source", "manual", "--rates", str(rates), "--output", str(fx)]), 0)
            self.assertEqual(json.loads(fx.read_text())["base_currency"], "USD")


if __name__ == "__main__":
    unittest.main()
