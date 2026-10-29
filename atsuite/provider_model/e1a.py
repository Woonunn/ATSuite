"""Read existing E1-A artifacts offline; importing never deploys or fetches cloud data."""

import copy
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from atsuite.analysis.model import ProviderMetric, RunContext
from atsuite.analysis.pricing import create_pricing_policy
from atsuite.provider_model.observation import BACKENDS
from atsuite.tracing.storage import read_text, trace_path
from atsuite.workload.experiment import load_capture
from atsuite.workload.profile import extract_profile, number


def _read(path: Path) -> dict:
    return json.loads(read_text(path))


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in read_text(path).splitlines() if line]


def _ns(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1e9)


def _numeric(value):
    try:
        result = float(value) if not isinstance(value, bool) else None
    except (ValueError, TypeError):
        return None
    return result if number(result) else None


def _sum(fields: list[dict], name: str) -> float | None:
    values = [_numeric(f.get(name)) for f in fields]
    return sum(values) if values and all(v is not None for v in values) else None


def _estimate(
    provider: str, metrics: list[dict], pool: dict, evidence: str
) -> dict | None:
    if not metrics or any(
        not m.get("evidence_refs") or m.get("provider") != provider for m in metrics
    ):
        return None
    scope = {
        "aws_lambda": "lambda_invocations",
        "aws_agentcore": "agentcore_session_usage",
        "ali_fc": "fc_active_invocations",
        "ali_agentrun": "fc_mcp_tool_requests",
    }[provider]
    normalized = []
    for row in metrics:
        metric = ProviderMetric.from_dict(row)
        fields = metric.fields
        if provider == "aws_lambda":
            if not number(fields.get("billed_duration_ms")) or not number(
                fields.get("memory_limit_mb")
            ):
                return None
        elif provider == "aws_agentcore":
            if metric.source != "agentcore_usage" or not all(
                number(fields.get(k)) for k in ("vcpu_hours", "memory_gb_hours")
            ):
                return None
        else:
            metric.provider = "ali"
            if provider == "ali_agentrun":
                # FC Session HTTP duration measures transport, not asynchronous Tool CPU.
                fields = {"duration_ms": 0, "cpu": 0, "memory": 0, "disk": 512}
            else:
                duration = _numeric(fields.get("duration_ms"))
                if duration is None:
                    return None
                if str(fields.get("is_cold_start")).lower() == "true":
                    cold = _numeric(fields.get("cold_start_latency_ms"))
                    if cold is None:
                        return None
                    duration += cold
                fields = {
                    "duration_ms": duration,
                    "cpu": pool["resources"].get("cpu"),
                    "memory": pool["resources"].get("memory_mb"),
                    "disk": 512,
                }
                if not all(number(fields[k]) for k in ("cpu", "memory")):
                    return None
            metric.fields = fields
        normalized.append(metric)
    context = RunContext(
        uid="",
        provider=provider,
        observability_provider="",
        benchmark="DataSciBench",
        trace="",
        family="",
        config_path="",
    )
    lines = create_pricing_policy(provider).price(context, metrics=normalized, state=[])
    if not lines:
        return None
    components = {}
    for line in lines:
        components[line.category] = components.get(line.category, 0) + line.amount
    return {
        "basis": "estimated",
        "scope": scope,
        "currency": BACKENDS[provider][2],
        "complete": False,
        "components": components,
        "evidence": [evidence],
        "pricing": "repository PricingPolicy snapshot; excludes unobserved fees; not a vendor bill",
    }


def load_e1a(root: Path) -> tuple[list[dict], dict]:
    """Join frozen Capture features, request metrics and delayed provider evidence.

    Archived local availability is an event-time simulation at completion. Vendor
    metrics become available only at their recorded collection time. Unknown
    logical state is never reconstructed from transport bytes or archive sizes.
    """
    root = Path(root)
    repository = Path(__file__).resolve().parents[2]
    observations, sources, frozen, skipped = [], {}, set(), []
    global_session_owners = defaultdict(set)
    captures = {}
    run_paths = sorted(root.glob("*/run.json"))
    if (root / "run.json").exists():
        run_paths = [root / "run.json"]
    for run_path in run_paths:
        run = _read(run_path)
        provider = run["provider"]
        if provider not in BACKENDS:
            continue
        directory = run_path.parent
        records = _events(directory / "request_metrics.jsonl")
        image = run["deployment"]["image"]
        implementation = next(
            v
            for k, v in image["source_sha256"].items()
            if k.endswith("/implementation.py")
        )
        identity = {
            "capture": image["capture_events_sha256"],
            "tool": implementation,
            "data": sorted({r["data_version"] for r in records}),
            "graph": run["source_files"][
                "benchmarks/DataSciBench/langgraph_agent/graph.py"
            ],
            "replay": run["source_files"][
                "benchmarks/DataSciBench/langgraph_agent/trace_replayer.py"
            ],
        }
        frozen.add(json.dumps(identity, sort_keys=True))
        capture = (
            repository
            / "benchmarks/DataSciBench/trace/captured"
            / records[0]["trace_id"]
        )
        if capture not in captures:
            if (
                hashlib.sha256(read_text(capture / "events.jsonl").encode()).hexdigest()
                != identity["capture"]
            ):
                raise ValueError("Frozen capture checksum mismatch")
            captures[capture] = extract_profile(load_capture(capture))
        profile = captures[capture]
        actual = run["deployment"]["actual"]
        affinity = actual.get("session_affinity_config") or {}
        if isinstance(affinity, str):
            affinity = json.loads(affinity)
        pool = {
            "provider": provider,
            "environment": hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode()
            ).hexdigest(),
            "resources": {
                k: actual.get(k)
                for k in (
                    "region",
                    "cpu",
                    "memory_mb",
                    "disk_mb",
                    "timeout_s",
                    "lifecycle_configuration",
                    "reserved_concurrency",
                    "reserved_request_concurrency",
                )
            },
            "instances": actual.get("instances"),
            "sessions_per_instance": affinity.get("sessionConcurrencyPerInstance"),
            "invocations_per_instance": actual.get("instance_concurrency")
            if number(actual.get("instance_concurrency"))
            else None,
        }
        pool["resources"].update(image=image["id"], architecture=image["architecture"])
        pool["resources"]["session_affinity_config"] = affinity
        if actual.get("ephemeral_storage"):
            pool["resources"]["disk_mb"] = actual["ephemeral_storage"].get("Size")
        load = len(records) / (float(run["duration_ms"]) / 1000)
        proofs = [
            run_path,
            directory / "request_metrics.jsonl",
            trace_path(capture / "events.jsonl"),
        ]
        vendor_paths = sorted(
            p
            for p in directory.glob("provider_evidence*.json")
            if "early" not in p.name
        )
        vendors = sorted(
            [(_read(p), p) for p in vendor_paths],
            key=lambda pair: pair[0].get("collected_epoch", 0),
        )
        request_events = {
            r["arrival_id"]: _events(
                directory / "requests" / r["arrival_id"] / "events.jsonl"
            )
            for r in records
            if trace_path(
                directory / "requests" / r["arrival_id"] / "events.jsonl"
            ).exists()
        }
        proofs.extend(
            trace_path(directory / "requests" / rid / "events.jsonl")
            for rid in request_events
        )
        proofs.extend(
            trace_path(capture / name)
            for name in ("result.json", "trace.atg.json")
            if trace_path(capture / name).exists()
        )
        session_owners = Counter()
        for events in request_events.values():
            session_owners.update(
                {
                    e.get("transport", {})
                    .get("provider_metadata", {})
                    .get("session_id")
                    for e in events
                    if e["kind"] == "tool"
                }
            )
        for record in records:
            if not record.get("request_end_utc") or not record.get(
                "request_arrival_utc"
            ):
                skipped.append(
                    {
                        "run": directory.name,
                        "id": record["arrival_id"],
                        "reason": "missing terminal timestamps",
                    }
                )
                continue
            measurements = {}
            for key, source in {
                "latency_s": "end_to_end_ms",
                "tool_s": "tool_execution_ms",
                "state_sync_s": "state_synchronization_ms",
                "harness_queue_s": "harness_queue_ms",
            }.items():
                measurements[key] = (
                    record[source] / 1000 if number(record.get(source)) else None
                )
            row = {
                "kind": "request",
                "id": directory.name + ":" + record["arrival_id"],
                "task_id": record.get("task_id", "e1a-fixed"),
                "logical_id": record["arrival_id"],
                "profile": profile,
                "status": "success" if record["status"] == "succeeded" else "failed",
                "pool": pool,
                "load_rps": load,
                "arrival_ns": _ns(record["request_arrival_utc"]),
                "completed_ns": _ns(record["request_end_utc"]),
                "available_ns": _ns(record["request_end_utc"]),
                "measurements": measurements,
                "cost": None,
            }
            observations.append(copy.deepcopy(row))
            events = request_events.get(record["arrival_id"], [])
            tools = [e for e in events if e["kind"] == "tool"]
            calls = {e["transport"]["request_id"] for e in tools}
            sessions = {
                e["transport"].get("provider_metadata", {}).get("session_id")
                for e in tools
            } - {None, ""}
            for session in sessions:
                global_session_owners[(provider, session)].add(row["id"])
            for vendor, path in vendors:
                if not number(vendor.get("collected_epoch")):
                    continue
                by_call = {
                    m["invocation_call_id"]: m
                    for m in vendor["metrics"]
                    if m.get("invocation_call_id")
                }
                matched = [by_call[c] for c in calls if c in by_call]
                fields = [m["fields"] for m in matched]
                enriched = copy.deepcopy(row)
                enriched["available_ns"] = max(
                    row["completed_ns"], int(vendor["collected_epoch"] * 1e9)
                )
                if calls and len(matched) == len(calls):
                    for key, source in {
                        "queue_s": "queue_delay_ms",
                        "schedule_s": "schedule_latency_ms",
                    }.items():
                        value = _sum(fields, source)
                        enriched["measurements"][key] = (
                            value / 1000 if value is not None else None
                        )
                    cold = []
                    for f in fields:
                        flag = str(f.get("is_cold_start")).lower()
                        value = _numeric(
                            f.get(
                                "init_duration_ms"
                                if provider == "aws_lambda"
                                else "cold_start_latency_ms"
                            )
                        )
                        cold.append(
                            0 if flag == "false" else value if flag == "true" else None
                        )
                    enriched["measurements"]["cold_start_s"] = (
                        sum(cold) / 1000 if cold and None not in cold else None
                    )
                    if provider in {"aws_lambda", "ali_fc"}:
                        occupancy = _sum(fields, "duration_ms")
                        initialization = enriched["measurements"]["cold_start_s"]
                        enriched["measurements"]["service_slot_s"] = (
                            occupancy / 1000 + initialization
                            if occupancy is not None and initialization is not None
                            else None
                        )
                    if provider != "aws_agentcore":
                        enriched["cost"] = _estimate(provider, matched, pool, str(path))
                if (
                    provider == "aws_agentcore"
                    and sessions
                    and all(session_owners[s] == 1 for s in sessions)
                ):
                    usage = [
                        m
                        for m in vendor["metrics"]
                        if m["source"] == "agentcore_usage"
                        and m["fields"].get("session_id") in sessions
                    ]
                    if len(usage) == len(sessions):
                        enriched["cost"] = _estimate(provider, usage, pool, str(path))
                        if enriched["cost"]:
                            enriched["cost"]["session_ids"] = sorted(sessions)
                observations.append(enriched)
        proofs.extend(vendor_paths)
        report_path = directory / "window_report.json"
        if report_path.exists():
            report = _read(report_path)
            completed = max(
                _ns(r["request_end_utc"]) for r in records if r.get("request_end_utc")
            )
            end = int((run["started_epoch"] + float(run["duration_ms"]) / 1000) * 1e9)
            observations.append(
                {
                    "kind": "window",
                    "id": directory.name,
                    "pool": pool,
                    "profile": profile,
                    "load_rps": load,
                    "status": "success",
                    "arrival_ns": int(run["started_epoch"] * 1e9),
                    "completed_ns": end,
                    "available_ns": max(end, completed),
                    "cost": None,
                    "measurements": {
                        "throughput_rps": report.get("observed_window_goodput_per_s")
                    },
                }
            )
            proofs.append(report_path)
        for path in proofs:
            sources[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    for row in observations:
        if row.get("cost") and any(
            len(global_session_owners[(row["pool"]["provider"], sid)]) > 1
            for sid in row["cost"].get("session_ids", [])
        ):
            row["cost"] = None
    if not observations:
        raise ValueError("No E1-A observations found")
    if len(frozen) != 1:
        raise ValueError("Different frozen programs/datasets: validate separately")
    return observations, {
        "sources": sources,
        "frozen_environment": json.loads(next(iter(frozen))),
        "skipped_requests": skipped,
        "availability": "local completion-time simulation; provider collection timestamps retained",
        "limitations": [
            "Historical E1-A lacks complete logical S_G/rho and physical instance occupancy",
            "PricingPolicy subtotals are estimates, not complete actual charges",
            "Historical dependency versions are only as strong as recorded image/source evidence",
        ],
    }
