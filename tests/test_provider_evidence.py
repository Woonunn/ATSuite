"""Missing evidence stays unknown; activity samples are not full residence."""

import json
import tempfile
import unittest
from pathlib import Path

from atsuite.provider_model.evidence import residence_evidence
from atsuite.workload import extract_profile
from atsuite.workload.experiment import load_capture
from atsuite_sdk.events import event_payload, runtime_metadata
from experiments.exp1.runner.provider_usage import session_usage


class EvidenceTests(unittest.TestCase):
    def test_usage_samples_are_deduplicated_and_not_full_residence(self):
        def event(stamp, elapsed):
            return {
                "message": json.dumps(
                    {
                        "event_timestamp": stamp,
                        "attributes": {
                            "session.id": "s",
                            "time_elapsed_seconds": elapsed,
                        },
                    }
                )
            }

        a, b = event(1000, 1), event(4000, 0.5)
        usage = session_usage([a, a, b], "s")
        self.assertEqual(usage["residence"]["reported_usage_seconds"], 1.5)
        self.assertEqual(usage["residence"]["last_usage_epoch_ms"], 4000)
        self.assertFalse(usage["residence"]["complete"])
        self.assertIsNone(
            session_usage([a, event(5000, None)], "s")["residence"][
                "reported_usage_seconds"
            ]
        )
        self.assertIsNone(session_usage([], "s")["residence"]["first_usage_epoch_ms"])

    def test_only_matched_fc_activity_is_counted(self):
        row = {
            "requestId": "r",
            "instanceId": "i",
            "invokeFunctionStartTimestamp": "1000",
            "durationMs": "20",
        }
        vendor = {
            "metrics": [{"provider_request_id": "r"}],
            "evidence": [
                {
                    "source": "sls",
                    "raw": [
                        row,
                        row,
                        {**row, "requestId": "other", "instanceId": "outside"},
                    ],
                }
            ],
        }
        result = residence_evidence(vendor)
        self.assertEqual(list(result["fc_instances"]), ["i"])
        self.assertEqual(result["fc_instances"]["i"]["matched_invocations"], 1)
        self.assertEqual(result["fc_instances"]["i"]["observed_activity_span_s"], 0.02)
        self.assertFalse(result["complete_billable_residence"])

    def test_process_identity_is_stable_but_not_billable_instance(self):
        a, b = runtime_metadata(), event_payload("runtime_stopped")["runtime"]
        self.assertEqual(a["process_id"], b["process_id"])
        self.assertGreaterEqual(b["observed_uptime_s"], a["observed_uptime_s"])
        self.assertEqual(b["scope"], "application_process_not_billable_instance")

    def test_legacy_import_retains_only_complete_logical_accesses(self):
        events = [
            {
                "kind": "tool",
                "tool_name": "work",
                "monotonic_ns": 2000000000,
                "duration_s": 1,
                "request_id": "r",
                "session_id": "s",
                "utc": "2026-01-01T00:00:01+00:00",
                "result": {
                    "logical_state": {
                        "complete": True,
                        "accesses": [
                            {"object_id": "x", "operation": "read", "bytes": 512}
                        ],
                    }
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "result.json").write_text('{"status":"complete"}')
            (path / "trace.atg.json").write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"id": 1, "name": "work", "type": "tool_use", "edge_to": []}
                        ]
                    }
                )
            )
            for complete in (True, False):
                events[0]["result"]["logical_state"]["complete"] = complete
                (path / "events.jsonl").write_text(json.dumps(events[0]) + "\n")
                profile = extract_profile(load_capture(path))
                self.assertEqual(
                    profile["metrics"]["state_bytes"], 512 if complete else None
                )


if __name__ == "__main__":
    unittest.main()
