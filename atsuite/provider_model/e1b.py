"""Read complete E1-B attempts without turning replay outcomes into input features."""

import copy
import hashlib
import json
from pathlib import Path

from atsuite.provider_model.e1a import _events, _ns, _read
from atsuite.provider_model.observation import execution_context, validate_observation
from atsuite.tracing.storage import read_text, trace_path
from atsuite.workload.profile import extract_profile
from atsuite.workload.profile import number


def load_e1b(root: Path) -> tuple[list[dict], dict]:
    """Import terminal records; missing terminal evidence is an explicit error.

    Profiles become visible at completion, never at the current request's arrival.
    Costs and provider queue/cold-start observations stay unknown unless evidenced.
    """
    observations, sources = [], {}
    for path in sorted(Path(root).glob("*/run.json")):
        run = _read(path)
        if (
            run.get("experiment") != "E1-B"
            or run.get("pilot")
            or run.get("validation_only")
        ):
            continue
        metric_path = path.parent / "request_metrics.jsonl"
        records = _events(metric_path)
        if len(records) != run["requests"]:
            raise ValueError("Incomplete E1-B attempt table")
        actual, image = run["deployment"]["actual"], run["deployment"]["image"]
        identity = {
            "image": image["id"],
            "sources": image["source_sha256"],
            "workload": run["workload_manifest_sha256"],
            "references": run.get("environment_references", {}),
        }
        affinity = actual.get("session_affinity_config") or {}
        if isinstance(affinity, str):
            affinity = json.loads(affinity)
        pool = {
            "provider": run["provider"],
            "environment": hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode()
            ).hexdigest(),
            "resources": {
                **actual,
                "image": image["id"],
                "architecture": image["architecture"],
            },
            "instances": actual.get("instances"),
            "sessions_per_instance": affinity.get("sessionConcurrencyPerInstance"),
            "invocations_per_instance": actual.get("instance_concurrency")
            if number(actual.get("instance_concurrency"))
            else None,
        }
        for record in records:
            if not record.get("request_arrival_utc") or not record.get(
                "request_end_utc"
            ):
                raise ValueError("Missing terminal timestamps: " + record["arrival_id"])
            arrival, completed = (
                _ns(record["request_arrival_utc"]),
                _ns(record["request_end_utc"]),
            )
            rid = str(path.parent.resolve()) + ":" + record["arrival_id"]
            profile = copy.deepcopy(record.get("workload_profile")) or {
                "metrics": {},
                "workflow": {},
                "status": "failed",
            }
            profile.update(request_id=rid, arrival_ns=arrival, completed_ns=completed)
            trace_file = path.parent / "requests" / record["arrival_id"] / "trace.json"
            if trace_path(trace_file).exists():
                sources[str(trace_path(trace_file))] = hashlib.sha256(
                    trace_path(trace_file).read_bytes()
                ).hexdigest()
                timings = extract_profile(json.loads(read_text(trace_file)))
                for field in (
                    "tool_active_s",
                    "execution_active_s",
                    "runtime_tool_s",
                    "state_transfer_bytes",
                ):
                    profile["metrics"][field] = timings["metrics"].get(field)
                profile["runtime_tool_durations"] = timings["runtime_tool_durations"]
            profile["execution_pool"] = execution_context(pool)
            status = "success" if record["status"] == "succeeded" else "failed"
            profile["status"] = status
            row = {
                "kind": "request",
                "id": rid,
                "task_id": record["task_id"],
                "prompt_id": record.get("prompt_id"),
                "source_status": record["status"],
                "logical_id": record["arrival_id"],
                "profile": profile,
                "pool": pool,
                "error": record.get("error"),
                "graph_error": record.get("graph_error"),
                "application_cold_start": record.get("application_cold_start"),
                "load_rps": len(records) / (float(run["duration_ms"]) / 1000),
                "arrival_ns": arrival,
                "completed_ns": completed,
                "available_ns": completed,
                "status": status,
                "cost": None,
                "measurements": {
                    key: record[source] / 1000 if number(record.get(source)) else None
                    for key, source in {
                        "latency_s": "end_to_end_ms",
                        "tool_s": "tool_execution_ms",
                        "state_sync_s": "state_synchronization_ms",
                        "harness_queue_s": "harness_queue_ms",
                    }.items()
                },
            }
            validate_observation(row)
            observations.append(row)
        for source in (path, metric_path):
            sources[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
    if not observations:
        raise ValueError("No formal E1-B observations")
    return observations, {
        "sources": sources,
        "availability": "completion-time replay simulation",
        "limitations": [
            "No complete billing or inferred physical capacity",
            "Provider queue/cold-start enrichment not imported",
        ],
    }
