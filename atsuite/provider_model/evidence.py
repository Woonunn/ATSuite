"""Audit archived E1-A evidence offline, without inventing billable lifetimes."""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from atsuite.tracing.storage import read_text, trace_path
from atsuite.workload.experiment import load_capture
from atsuite.workload.profile import extract_profile, number
from experiments.exp1.runner.provider_usage import session_usage


def residence_evidence(vendor: dict) -> dict:
    """Recover usage samples and matched FC instance activity, not termination."""
    sessions = {}
    instances = defaultdict(list)
    matched = {m.get("provider_request_id") for m in vendor.get("metrics", [])} - {None}
    seen = set()
    for evidence in vendor.get("evidence", []):
        raw = evidence.get("raw")
        if evidence["source"] == "agentcore_usage" and isinstance(raw, dict):
            sid = raw.get("session_id")
            if sid:
                usage = session_usage(
                    [{"message": json.dumps(e)} for e in raw.get("raw_events", [])], sid
                )
                sessions[sid] = {"samples": usage["log_entries"], **usage["residence"]}
        elif evidence["source"] == "sls" and isinstance(raw, list):
            for row in raw:
                row = json.loads(row["_col0"]) if "_col0" in row else row
                if not isinstance(row, dict):
                    continue
                row = {k.lower(): v for k, v in row.items()}
                rid, instance = row.get("requestid"), row.get("instanceid")
                if rid not in matched or rid in seen or not instance:
                    continue
                try:
                    start = float(row["invokefunctionstarttimestamp"])
                    duration = float(row["durationms"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not number(start) or not number(duration):
                    continue
                seen.add(rid)
                instances[instance].append((start, start + duration))
    return {
        "agentcore_sessions": sessions,
        "fc_instances": {
            key: {
                "matched_invocations": len(intervals),
                "first_invoke_epoch_ms": min(a for a, _ in intervals),
                "last_invoke_end_epoch_ms": max(b for _, b in intervals),
                "observed_activity_span_s": (
                    max(b for _, b in intervals) - min(a for a, _ in intervals)
                )
                / 1000,
            }
            for key, intervals in instances.items()
        },
        "complete_billable_residence": False,
        "limitations": "Sample/activity bounds only; no complete creation, idle, termination or billable instance inventory. Do not sum across overlapping windows.",
    }


def audit(root: Path, bills: list[Path]) -> dict:
    """Retain source hashes and missing coverage; never allocate a daily bill."""
    sources, windows = {}, []

    def record(path):
        path = trace_path(path)
        sources[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return json.loads(read_text(path))

    for run_path in sorted(root.glob("*/run.json")):
        run = record(run_path)
        directory = run_path.parent
        metrics_path = trace_path(directory / "request_metrics.jsonl")
        sources[str(metrics_path)] = hashlib.sha256(
            metrics_path.read_bytes()
        ).hexdigest()
        records = [
            json.loads(line) for line in read_text(metrics_path).splitlines() if line
        ]
        coverage = {"state_bytes": 0, "llm_idle_s": 0, "rho_kb_s": 0}
        for row in records:
            request = directory / "requests" / row["arrival_id"]
            profile = row.get("workload_profile")
            if trace_path(request / "workload_profile.json").exists():
                profile = record(request / "workload_profile.json")
            elif profile is None and trace_path(request / "result.json").exists():
                profile = extract_profile(load_capture(request))
                for name in (
                    "events.jsonl",
                    "result.json",
                    "trace.json",
                    "trace.atg.json",
                ):
                    p = trace_path(request / name)
                    if p.exists():
                        sources[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
            for key in coverage:
                # A failed request with no Tool spans can have zero observed bytes.
                coverage[key] += bool(
                    profile
                    and profile.get("status") == "success"
                    and number(profile.get("metrics", {}).get(key))
                )
        vendors = [
            (record(p), p)
            for p in directory.glob("provider_evidence*.json")
            if "early" not in p.name
        ]
        vendor, path = max(
            vendors,
            key=lambda pair: pair[0].get("collected_epoch", 0),
            default=({}, None),
        )
        windows.append(
            {
                "run": directory.name,
                "provider": run["provider"],
                "requests": len(records),
                "successful_logical_metric_coverage": coverage,
                "provider_evidence": str(path) if path else None,
                "residence": residence_evidence(vendor),
            }
        )
    billing = [
        {
            "path": str(p),
            **{
                k: v
                for k, v in record(p).items()
                if k in ("basis", "scope", "queried_utc", "attribution_status")
            },
        }
        for p in bills
    ]
    return {
        "windows": windows,
        "billing": billing,
        "sources": sources,
        "complete_request_cost_verified": False,
        "billing_limitation": "Daily snapshots remain unallocated; no assertion of final settlement or full cost coverage.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--bill", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root, args.bill)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"windows": len(result["windows"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
