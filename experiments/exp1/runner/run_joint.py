"""Audited, budget-reserved mixed-cloud Replay with joint capacity control."""

import argparse
import asyncio
import hashlib
import json
import signal
import time
from dataclasses import fields
from pathlib import Path

from dotenv import load_dotenv

from atsuite.routing import ScalingAdapter
from atsuite.routing.experiment import build_controller, control_identity
from experiments.exp1.capture.capture_agent_traces import run as replay
from experiments.exp1.data.prepare_e1b import PROVIDERS, ROOT, load, sha, write_csv
from experiments.exp1.runner.arrival_scheduler import Arrival, run_open_loop, stats
from experiments.exp1.runner.e1b_references import load_references
from experiments.exp1.runner.run_e1b_cloud import (
    pilot_rows,
    validate_deployment,
    validate_pilots,
)
from experiments.exp1.runner.static_deployment_runner import reserve_budget


def experiment_identity(config: dict) -> str:
    """Bind a mixed Pilot to the actual frozen controller and model inputs."""
    keys = ("harness_concurrency", "tool_rpc_timeout_s")
    value = {k: config.get(k) for k in keys}
    value["control_identity"] = control_identity(config)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def cloud_adapters(entries: list[dict], config: dict) -> dict:
    """Use the repository's provider clients against audited resource identities."""
    import boto3
    from botocore.config import Config

    from atsuite.ali.ali import Ali

    ali = Ali()
    adapters = {}
    for entry in entries:
        provider, actual = entry["provider"], entry["actual"]
        if provider.startswith("aws_"):
            client = boto3.client(
                "lambda" if provider == "aws_lambda" else "bedrock-agentcore-control",
                region_name=actual["region"],
                config=Config(
                    connect_timeout=5, read_timeout=20, retries={"max_attempts": 1}
                ),
            )
        else:
            client = ali.get_fc_client()
        # Existing endpoints invoke the unqualified function; PC requires a separately audited alias deployment.
        if config["pools"][provider].get("qualifier"):
            raise ValueError(
                "This E1-B endpoint manifest has no audited qualified alias"
            )
        adapters[provider] = ScalingAdapter(
            provider, client, actual.get("runtime_id") or actual["runtime_name"]
        )
    return adapters


def validate_inputs(
    config: dict,
    workload: Path,
    deployment: Path,
    pilots: Path,
    references: Path,
    *,
    pilot: bool,
) -> tuple:
    """Verify existing sealed workload, deployments, Pilot and Stage 5 evidence hashes."""
    manifest, rows = load(workload)
    if len({(r.get("scenario"), r.get("window_role")) for r in rows}) != 1:
        raise ValueError("Select exactly one sealed scenario/window for each cloud run")
    entries = json.loads(deployment.read_text())["results"]
    if len(entries) != 4 or {e["provider"] for e in entries} != set(PROVIDERS):
        raise ValueError("All four audited backends are required")
    if set(config["pools"]) != set(PROVIDERS):
        raise ValueError("Control configuration must describe the four audited pools")
    for entry in entries:
        validate_deployment(manifest, workload, entry)
        for level in config["pools"][entry["provider"]]["levels"]:
            resources = level["pool"]["resources"]
            controlled = (
                "reserved_concurrency"
                if entry["provider"] == "aws_lambda"
                else "min_instances"
                if entry["provider"].startswith("ali_")
                else None
            )
            # Runtime configuration, including Session lifetimes, is frozen outside the capacity control.
            if any(
                resources.get(k) != v
                for k, v in entry["actual"].items()
                if k != controlled
            ):
                raise ValueError("Capacity evidence belongs to another deployment")
            if controlled and resources.get(controlled) != level["target"]:
                raise ValueError("Capacity target and resource identity disagree")
    refs = load_references(references, manifest, deployment)
    if any(set(refs.get(p, {})) != set(manifest["tasks"]) for p in PROVIDERS):
        raise ValueError(
            "Every task/pool needs an environment-specific sealed reference"
        )
    evidence = validate_pilots(pilots, workload, deployment, manifest["tasks"], refs)
    if not config.get("evidence"):
        raise ValueError(
            "Independent simulation and capacity calibration evidence required"
        )
    for item in config["evidence"]:
        if sha(ROOT / item["path"]) != item["sha256"]:
            raise ValueError("Stage 5 evidence changed")
    required_evidence = {config["simulation_report"]}
    if not pilot:
        required_evidence.add(config["joint_pilot_report"])
    if not required_evidence <= {item["path"] for item in config["evidence"]}:
        raise ValueError(
            "Simulation and mixed Pilot reports must be hash-sealed evidence"
        )
    simulation = json.loads((ROOT / config["simulation_report"]).read_text())
    if (
        simulation.get("valid") is not True
        or simulation.get("control_identity") != control_identity(config)
        or simulation.get("provenance", {})
        .get("sealed_workload", {})
        .get("manifest_sha256")
        != sha(workload / "manifest.json")
        or simulation.get("synthetic") is not False
        or simulation.get("policy") != "joint"
        or not simulation.get("scaling_events")
        or simulation.get("admitted", 0) == 0
        or simulation.get("unsatisfied_rate_offered", 1)
        > config.get("risk", {}).get("max_risk", 0.05)
        or not simulation.get("validation")
        or not all(r["passed"] for r in simulation["validation"])
    ):
        raise ValueError("A measured, independently validated simulation is required")
    if not pilot:
        mixed = json.loads((ROOT / config["joint_pilot_report"]).read_text())
        if (
            mixed.get("status") != "complete"
            or mixed.get("pilot") is not True
            or mixed.get("config_sha256") != experiment_identity(config)
            or mixed.get("deployment_sha256") != sha(deployment)
            or mixed.get("workload_sha256") != sha(workload / "manifest.json")
            or mixed.get("all_successful") is not True
            or mixed.get("scaling_effective", 0) < 1
        ):
            raise ValueError(
                "Formal joint run requires a successful matching mixed Pilot"
            )
    return manifest, pilot_rows(rows) if pilot else rows, entries, refs, evidence


async def execute(
    config: dict,
    workload: Path,
    deployment: Path,
    pilots: Path,
    references: Path,
    output: Path,
    *,
    pilot=False,
    preflight_only=False,
    policy="joint",
) -> dict:
    """Run one declared policy; restore the initial target after confirmed Session drain."""
    manifest, rows, entries, refs, evidence = validate_inputs(
        config, workload, deployment, pilots, references, pilot=pilot
    )
    output.mkdir(parents=True, exist_ok=False)
    adapters = cloud_adapters(entries, config)
    controller = build_controller(
        config, adapters, policy=policy, log_path=output / "control.jsonl"
    )
    preview = await asyncio.to_thread(
        controller.tick, config["initial_task_rates"], apply=False
    )
    initial = {k: s.target for k, s in controller.snapshots.items()}
    if any(initial.get(k) != v["initial"] for k, v in config["pools"].items()):
        raise ValueError(
            "Live capacity differs from the calibrated initial configuration"
        )
    report = {
        "status": "preflight",
        "pilot": pilot,
        "policy": policy,
        "config_sha256": experiment_identity(config),
        "deployment_sha256": sha(deployment),
        "workload_sha256": sha(workload / "manifest.json"),
        "pilot_evidence": evidence,
        "initial_capacity": initial,
        "preview": preview,
        "actual_cost": None,
        "started_ns": time.time_ns(),
    }
    report_path = output / "run.json"

    def save():
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")

    save()
    if preflight_only or preview["selected"] is None:
        report["status"] = (
            "ready" if preview["selected"] else "blocked_no_trusted_feasible_plan"
        )
        save()
        return report
    # Reserve the complete run, cold/idle tail, restoration and ancillary charges before any mutation/RPC.
    reservations = []
    for provider in ("aws_lambda", "ali_fc"):
        vendor = "aws" if provider.startswith("aws") else "ali"
        bound = config["budget_reservations"][vendor]
        reservations.append(
            reserve_budget(
                ROOT / "results/phase4/budget_ledger.json",
                config,
                provider,
                hashlib.sha256((str(output.resolve()) + vendor).encode()).hexdigest(),
                bound["amount"],
                bound["basis"],
            )
        )
    report["reservations"] = reservations
    save()
    base = json.loads(
        (ROOT / "benchmarks/DataSciBench/config/langgraph_experiment.json").read_text()
    )
    targets = {}
    for entry in entries:
        provider = entry["provider"]
        endpoints = json.loads(
            (ROOT / "url_results" / Path(entry["config"]).name).read_text()
        )
        if endpoints["provider"] != provider or len(endpoints["targets"]) != 1:
            raise ValueError("Invalid audited endpoint")
        target_id, endpoint = next(iter(endpoints["targets"].items()))
        targets[provider] = {
            "provider": provider,
            "family": endpoint["family"],
            "target_id": target_id,
            "endpoint": endpoint["endpoint"],
            "timeout": config["tool_rpc_timeout_s"],
            "expected_implementation_sha256": next(
                v
                for k, v in entry["image"]["source_sha256"].items()
                if k.endswith("/implementation.py")
            ),
            "execution_scope": "Stage 5 controlled mixed-cloud Replay",
        }
    write_csv(output / "planned_requests.csv", rows)
    by_id, failed = {r["arrival_id"]: r for r in rows}, False
    stopped = asyncio.Event()
    offered, outcomes = {}, []
    queue_depth = 0
    run_id = hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:16]

    def observe(event):
        nonlocal queue_depth
        if "queue_depth" in event:
            queue_depth = event["queue_depth"]
        aid = event.get("arrival_id")
        if aid is None:
            return
        rid = run_id + ":" + aid
        if event["kind"] == "request_submitted" or (
            event["kind"] == "request_terminal" and event["status"] == "rejected"
        ):
            offered[aid] = event["wall_ns"]
            controller.observe_arrival(rid, by_id[aid]["task_id"], event["wall_ns"])
        if event["kind"] == "request_terminal":
            decision = controller.decisions.get(rid, {})
            elapsed = (
                event["completed_monotonic_ns"] - event["planned_arrival_monotonic_ns"]
            ) / 1e9
            row = {
                "arrival_id": aid,
                "request_id": rid,
                "task_id": by_id[aid]["task_id"],
                "pool": decision.get("selected"),
                "status": event["status"],
                "latency_s": elapsed,
                "error": event.get("error"),
                "slo_violated": event["status"] != "succeeded"
                or elapsed > controller.router.slo_s,
            }
            outcomes.append(row)
            with (output / "request_metrics.jsonl").open("a") as stream:
                stream.write(json.dumps(row, allow_nan=False) + "\n")

    async def control_loop():
        while not stopped.is_set():
            rates = controller.observed_rates() or (
                dict.fromkeys(config["initial_task_rates"], 0.0)
                if controller.offered_ids
                else config["initial_task_rates"]
            )
            await asyncio.to_thread(controller.tick, rates, queued=queue_depth)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=controller.period_s)
            except TimeoutError:
                pass

    async def handler(arrival, started):
        nonlocal failed
        started()
        if failed and pilot:
            raise RuntimeError("Previous joint Pilot request failed")
        if loop_task.done():
            loop_task.result()
            raise RuntimeError("Control loop stopped")
        row = by_id[arrival.arrival_id]
        task = row["task_id"]
        selected_targets = {
            p: {"tool": tool, "capture": Path(refs[p][task]["capture"])}
            for p, tool in targets.items()
        }
        try:
            return await replay(
                task,
                {**base, "tool": targets[PROVIDERS[0]]},
                output / "requests" / arrival.arrival_id,
                router=controller,
                routing_context={
                    "targets": selected_targets,
                    "request_id": run_id + ":" + arrival.arrival_id,
                    "arrival_ns": offered[arrival.arrival_id],
                    "load_rps": sum(
                        (
                            controller.observed_rates() or config["initial_task_rates"]
                        ).values()
                    ),
                },
            )
        except BaseException:
            failed = True
            raise

    loop_task = asyncio.create_task(control_loop())
    report["status"] = "running"
    save()
    try:
        arrivals = [
            Arrival(**{f.name: r[f.name] for f in fields(Arrival)}) for r in rows
        ]
        duration = (
            float(rows[-1]["relative_arrival_ms"]) + 1000
            if pilot
            else manifest["config"]["window_duration_ms"]
        )
        summary = await run_open_loop(
            arrivals,
            handler,
            output / "schedule",
            duration_ms=duration,
            concurrency=config["harness_concurrency"],
            queue_capacity=manifest["config"]["queue_capacity"],
            metadata=report,
            observer=observe,
        )
        report["all_successful"] = summary["terminal_counts"].get(
            "succeeded", 0
        ) == len(rows)
        report["status"] = "complete" if report["all_successful"] else "failed"
        report["schedule"] = summary
    except BaseException as exc:
        report["status"] = (
            "interrupted"
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
            else "failed"
        )
        report["error_type"] = type(exc).__name__
        raise
    finally:
        stopped.set()
        await asyncio.gather(loop_task, return_exceptions=True)
        while controller.retained_until:
            controller.expire_retained(time.time_ns())
            if controller.retained_until:
                await asyncio.sleep(
                    min(
                        1,
                        max(
                            0,
                            (min(controller.retained_until.values()) - time.time_ns())
                            / 1e9,
                        ),
                    )
                )
        restoration = {}
        for key, adapter in adapters.items():
            if adapter.control == "managed":
                continue
            if controller._occupied(key):
                restoration[key] = "blocked_by_active_or_unconfirmed_session"
                continue
            try:
                await asyncio.to_thread(adapter.apply, initial[key], active_sessions=0)
                state = await asyncio.to_thread(adapter.read)
                deadline = time.monotonic() + 120
                while (
                    state.status != "READY" or state.effective != initial[key]
                ) and time.monotonic() < deadline:
                    await asyncio.sleep(1)
                    state = await asyncio.to_thread(adapter.read)
                restoration[key] = state.to_dict()
                if state.status != "READY" or state.effective != initial[key]:
                    restoration[key]["restoration_unconfirmed"] = True
            except Exception as exc:  # noqa: BLE001 - retain cleanup failures for every provider.
                restoration[key] = {"error": type(exc).__name__}
        report["restoration"] = restoration
        report["scaling_effective"] = sum(
            json.loads(line).get("event") == "scaling_effective"
            for line in (output / "control.jsonl").read_text().splitlines()
        )
        if any(
            isinstance(value, str)
            or value.get("error")
            or value.get("restoration_unconfirmed")
            for value in restoration.values()
        ):
            report["status"] = "cleanup_incomplete"
        admitted = [r for r in outcomes if r["pool"]]
        success = [r for r in outcomes if r["status"] == "succeeded"]
        report["metrics"] = {
            "offered": len(rows),
            "admitted": len(admitted),
            "success": len(success),
            "admission_rate": len(admitted) / len(rows),
            "success_rate": len(success) / len(rows),
            "p95_s": stats([r["latency_s"] for r in success])["p95"],
            "unsatisfied_rate_offered": sum(r["slo_violated"] for r in outcomes)
            / len(rows),
            "slo_violation_rate_admitted": sum(r["slo_violated"] for r in admitted)
            / len(admitted)
            if admitted
            else None,
            "actual_total_cost": None,
        }
        report["finished_ns"] = time.time_ns()
        controller.router.provider.save(output / "provider_model.json")
        controller.router.workload.save(output / "workload_model.json")
        save()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("config", "workload", "deployment", "pilots", "references", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--policy",
        choices=("static", "routing", "independent", "joint"),
        default="joint",
    )
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    config = json.loads(args.config.read_text())

    async def launch():
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGTERM, asyncio.current_task().cancel
        )
        return await execute(
            config,
            args.workload,
            args.deployment,
            args.pilots,
            args.references,
            args.output,
            pilot=args.pilot,
            preflight_only=args.preflight_only,
            policy=args.policy,
        )

    asyncio.run(launch())


if __name__ == "__main__":
    main()
