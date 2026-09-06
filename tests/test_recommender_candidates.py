import json
import tempfile
import unittest
from pathlib import Path

from atsuite.pipeline import PROVIDERS, provider_cloud_name, resolve_benchmark
from atsuite.recommender.candidates import generate_candidates


def make_config(root: Path, families=("faas", "session")) -> Path:
    (root / "nodes" / "tool").mkdir(parents=True)
    (root / "trace").mkdir()
    (root / "config").mkdir()
    (root / "trace" / "trace.json").write_text(json.dumps({"nodes": [
        {"id": 0, "name": "start", "type": "logic", "edge_to": [{"id": 1}]},
        {"id": 1, "name": "tool.run", "type": "tool_use", "edge_to": []},
    ]}))
    pipeline = {
        "faas": {"units": [{"name": "tool", "node": "tool", "trace_names": ["tool.run"], "deploy": {"cpu": 1, "memory": 512}}]},
        "session": {"servers": [{"name": "server", "nodes": ["tool"], "deploy": {"cpu": 2, "memory": 2048}}]},
    }
    config = root / "config" / "bench.json"
    config.write_text(json.dumps({
        "trace_file": "./trace/trace.json",
        "nodes": [{"name": "tool", "dir": "./nodes/tool", "trace_names": ["tool.run"]}],
        "pipeline": {key: pipeline[key] for key in families},
    }))
    return config


class CandidateTests(unittest.TestCase):
    def tearDown(self):
        resolve_benchmark.cache_clear()

    def test_families_and_registry(self):
        for families in (("faas",), ("session",), ("faas", "session")):
            with tempfile.TemporaryDirectory() as tmp:
                candidates = generate_candidates(make_config(Path(tmp), families))
                self.assertEqual(len(candidates), 6)
                for candidate in candidates:
                    self.assertEqual(candidate.family, PROVIDERS[candidate.provider].family)
                    self.assertEqual(candidate.cloud, provider_cloud_name(candidate.provider))
                    self.assertEqual(candidate.supported, candidate.family in families)
                    if candidate.supported:
                        self.assertEqual(candidate.deployment_shape["target_count"], 1)
                        self.assertEqual(candidate.deployment_shape["total_cpu"], 1 if candidate.family == "faas" else 2)
                    else:
                        self.assertTrue(candidate.unsupported_reason)

    def test_filter_gateway_and_unknown_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            candidates = generate_candidates(config, ["AWS_LAMBDA", "aws_lambda", "mcp_gateway", "unknown"])
            self.assertEqual([c.id for c in candidates], ["aws_lambda", "mcp_gateway", "unknown"])
            self.assertTrue(candidates[1].supported)
            self.assertFalse(candidates[2].supported)
            self.assertEqual(generate_candidates(config, []), [])
