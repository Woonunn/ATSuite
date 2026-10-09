import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from atsuite.recommender.engine import RecommendationEngine
from atsuite.recommender.api import write_plan
from atsuite.recommender.contracts import ExperimentSpec
from atsuite.recommender.export import recommendation_dict
from atsuite.recommender.model import RecommendationConstraints
from atsuite.recommender.scoring import OBJECTIVES, validate_weights


def comma_list(value):
    values = [item.strip().lower() for item in value.split(",")]
    if not all(values):
        raise argparse.ArgumentTypeError("Use a nonempty comma-separated list")
    return values


def parse_weights(value):
    weights = {}
    try:
        for item in value.split(","):
            key, amount = item.split("=", 1)
            key = key.strip()
            if key in weights:
                raise ValueError(f"Duplicate weight: {key}")
            weights[key] = float(amount)
        validate_weights(weights)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return weights


def load_fx(path):
    if path is None:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("base_currency") != "USD" or not isinstance(payload.get("rates"), dict):
        raise ValueError("FX config requires base_currency=USD and a rates object")
    return payload["rates"]


@contextlib.contextmanager
def execution_output_to_stderr():
    """Keep Python progress and inherited Docker/deployer stdout out of JSON."""
    saved = None
    try:
        try:
            stdout_fd, stderr_fd = sys.stdout.fileno(), sys.stderr.fileno()
        except (AttributeError, OSError):
            stdout_fd = stderr_fd = None
        if stdout_fd is not None:
            sys.stdout.flush()
            saved = os.dup(stdout_fd)
            os.dup2(stderr_fd, stdout_fd)
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        if saved is not None:
            os.dup2(saved, stdout_fd)
            os.close(saved)


def format_recommendation(result):
    def number(value, digits=2):
        return f"{value:.{digits}f}" if value is not None else "N/A"

    lines = ["ATSuite Recommendation", f"Benchmark: {result.benchmark}",
             f"Candidates requested: {len(result.candidates)}; executed: {result.experiment['executed_candidates']}; comparable: {result.experiment['comparable_candidates']}", ""]
    headers = ["Candidate", "Family", "P50 ms", "P95 ms", "Cost USD", "Failure", "Score", "Selection"]
    table = [headers]
    ordered = result.rankings + [row for row in result.candidates if row not in result.rankings]
    for row in ordered:
        measurement = row.measurement
        cost = row.metrics["cost"]
        table.append([
            row.candidate.id, row.candidate.family or "N/A",
            number(measurement.latency_ms.p50), number(measurement.latency_ms.p95),
            number(cost.value, 7) if cost.status == "available" else cost.status,
            f"{100 * measurement.failure_rate:.1f}%" if measurement.failure_rate is not None else "N/A",
            number(row.score, 3), "excluded" if row.exclusion_reasons else "Pareto" if row.on_pareto_frontier else "dominated",
        ])
    widths = [max(len(row[index]) for row in table) for index in range(len(headers))]
    lines.extend("  ".join(cell.ljust(width) for cell, width in zip(row, widths)) for row in table)
    if result.winner:
        winner = result.winner
        stats = winner.measurement.latency_ms
        family = "FaaS" if winner.candidate.family == "faas" else "Session-MCP"
        lines.extend(["", "Recommended deployment", f"Provider: {winner.candidate.provider}",
                      f"Cloud: {winner.candidate.cloud}", f"Deployment: {family}",
                      f"Score: {winner.score:.3f}", f"Confidence: {result.confidence}",
                      f"Score margin: {number(result.score_margin, 3)}",
                      f"Latency: mean {number(stats.mean)} ms; median {number(stats.median)} ms; stddev {number(stats.stddev)} ms; CV {number(stats.cv, 3)}",
                      f"Memory: {number(winner.metrics['memory'].value)} MB ({winner.metrics['memory'].status})",
                      f"State synchronization: {number(winner.metrics['state_overhead'].value)} ms ({winner.metrics['state_overhead'].status})",
                      f"Cold start rate: {number(winner.metrics['cold_start'].value, 4)} ({winner.metrics['cold_start'].status})"])
        for label, provider in (("latency", result.best_latency), ("cost", result.best_cost),
                                ("reliability", result.best_reliability), ("balanced", result.best_balanced),
                                ("FaaS", result.best_faas), ("Session-MCP", result.best_session)):
            lines.append(f"Best {label}: {provider or 'unavailable'}")
    else:
        lines.extend(["", "Recommendation unavailable"])
    lines.append("")
    lines.extend(f"- {message}" for message in result.explanation)
    for row in result.candidates:
        for reason in row.exclusion_reasons:
            lines.append(f"{row.candidate.id}: excluded — {reason}")
        shape = row.candidate.deployment_shape
        if shape:
            lines.append(f"{row.candidate.id} configured shape: {shape['target_count']} targets, {shape['total_cpu']} CPU, {shape['total_memory_mb']} MB")
        for diagnostic in row.measurement.diagnostics:
            if diagnostic.get("kind") != "metric_unavailable":
                lines.append(f"{row.candidate.id}: {diagnostic.get('kind')} — {diagnostic.get('error') or diagnostic.get('reason') or diagnostic}")
        lines.append(f"{row.candidate.id}: cleanup={row.measurement.cleanup_status}")
    lines.append(f"Report: {result.report_path}")
    return "\n".join(lines)


def _new_cli(argv):
    command = argv[0]
    parser = argparse.ArgumentParser(prog=f"atsuite-recommend {command}")
    if command == "plan":
        parser.add_argument("--spec", required=True, type=Path)
        parser.add_argument("--output", required=True, type=Path)
        parser.add_argument('--market-snapshot',type=Path)
        parser.add_argument('--request',help='optional user intent; unresolved intent blocks execution approval')
        args = parser.parse_args(argv[1:])
        spec = ExperimentSpec.from_dict(json.loads(args.spec.read_text(encoding="utf-8")))
        from atsuite.recommender import service
        snapshot=json.loads(args.market_snapshot.read_text()) if args.market_snapshot else None
        if args.request:
            from atsuite.advisor.workflow import interpret_request
            from dataclasses import asdict
            intent=interpret_request(args.request,asdict(spec.objective))
            payload=spec.to_dict();payload['objective']=intent['objective']
            if intent['allowed_providers']:payload['providers']=[p for p in payload['providers'] if p in intent['allowed_providers']]
            spec=ExperimentSpec.from_dict(payload)
            result=service.plan(spec,market_snapshot=snapshot);result['intent']=intent
            from atsuite.recommender.api import materialize_plan
            materialize_plan(result,args.output)
        else:service.plan(spec,args.output,market_snapshot=snapshot)
        print(args.output)
        return 0
    if command == "compare":
        parser.add_argument("--manifest", required=True, type=Path)
        parser.add_argument("--objective", type=Path)
        parser.add_argument("--output", type=Path)
        args = parser.parse_args(argv[1:])
        payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        from atsuite.recommender.service import compare_manifest
        from atsuite.recommender.contracts import ObjectiveSpec
        objective = ObjectiveSpec.from_dict(json.loads(args.objective.read_text())) if args.objective else None
        result = compare_manifest(payload, objective=objective, base_path=args.manifest.parent)
        output_text = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output_text, encoding="utf-8")
            print(args.output)
        else:
            print(output_text, end="")
        return 0
    if command == "reprice":
        parser.add_argument("--experiment", required=True, type=Path)
        parser.add_argument("--market-snapshot", required=True, type=Path)
        parser.add_argument("--output", required=True, type=Path)
        args = parser.parse_args(argv[1:])
        original = json.loads(args.experiment.read_text(encoding="utf-8"))
        market = json.loads(args.market_snapshot.read_text(encoding="utf-8"))
        from atsuite.recommender.identity import digest
        from atsuite.market_data.reprice import reprice
        ledger_ref = original.get("usage_ledger") or original.get("usage_ledger_path")
        if ledger_ref:
            ledger_path = Path(str(ledger_ref))
            if not ledger_path.is_absolute():
                ledger_path = args.experiment.parent / ledger_path
        else:
            ledger_path = args.experiment.with_name("usage_ledger.jsonl")
        if not ledger_path.is_file():
            result = {"schema_version": 1, "status": "insufficient_usage_ledger",
                      "original": str(args.experiment), "original_digest": digest(original, length=32),
                      "market_snapshot": market, "market_snapshot_digest": digest(market, length=32),
                      "usage_ledger": str(ledger_path),
                      "note": "Repricing requires the immutable usage_ledger.jsonl produced by the experiment."}
        else:
            result = reprice(original, market, usage_ledger_path=ledger_path,
                             target_currency=str((original.get("objective") or {}).get("base_currency", "USD")))
            result["original"] = str(args.experiment)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        print(args.output)
        return 0
    if command == "resume":
        parser.add_argument("--experiment", required=True)
        parser.add_argument("--store", default=Path("results/recommender/jobs.sqlite"))
        args = parser.parse_args(argv[1:])
        from atsuite.execution.postgres_store import open_store
        from atsuite.recommender.service import resume
        store = open_store(args.store)
        try:
            result = resume(store, args.experiment)
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if command == "run":
        parser.add_argument("--plan", required=True, type=Path)
        parser.add_argument("--approve-budget", action="store_true")
        parser.add_argument("--store", default=Path("results/recommender/jobs.sqlite"))
        parser.add_argument("--wait", action="store_true")
        parser.add_argument("--account", action="append", default=[], help="approved provider=account identity")
        args = parser.parse_args(argv[1:])
        if not args.approve_budget:
            parser.error("run requires --approve-budget")
        payload = json.loads(args.plan.read_text(encoding="utf-8"))
        from atsuite.execution.postgres_store import open_store
        from atsuite.recommender import service
        accounts = dict(item.split("=", 1) for item in args.account)
        store = open_store(args.store)
        try:
            approval = service.approve(store, payload, subject="local-cli", tenant="local", project="local", accounts=accounts)
            record = service.enqueue(store, payload, approval, tenant="local", project="local", subject="local-cli")
            if args.wait:
                with execution_output_to_stderr():
                    result = service.run(store, record['id'])
                print(json.dumps(result, ensure_ascii=False, default=str))
                return 0 if result['state'] == 'COMPLETED' else 1
        finally:
            store.close()
        print(json.dumps({"status":"execution_queued", "experiment":record['id'], "state":record['state'],"store":store.path,
                          "candidates":len(payload.get('candidates',[]))},ensure_ascii=False))
        return 0
    parser.error(f"unknown command: {command}")


def main(argv=None):
    from atsuite.config import load_local_env
    load_local_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"plan", "run", "resume", "compare", "reprice"}:
        return _new_cli(argv)
    parser = argparse.ArgumentParser(description="Benchmark deployment candidates and recommend a strategy from measured evidence.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--providers", type=comma_list, help="Default: all six first-party cloud runtimes")
    parser.add_argument("--include-provider", action="append", default=[], help="Opt into an extra provider, e.g. mcp_gateway")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--objective", choices=OBJECTIVES, default="balanced")
    parser.add_argument("--weights", type=parse_weights)
    parser.add_argument("--latency-stat", choices=["p50", "p95", "median"], default="p95")
    parser.add_argument("--max-p95-ms", type=float)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--max-failure-rate", type=float)
    parser.add_argument("--max-memory-mb", type=float)
    parser.add_argument("--cloud", type=comma_list)
    parser.add_argument("--family", type=comma_list, help="faas,session")
    parser.add_argument("--fx-config", type=Path)
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("results/recommendations"))
    parser.add_argument("--json", action="store_true", help="Print only the JSON report to stdout; progress goes to stderr")
    parser.add_argument("--approve-budget", action="store_true")
    parser.add_argument("--max-budget", type=float)
    parser.add_argument("--max-wall-time", type=int, default=7200)
    parser.add_argument("--store", default=Path("results/recommender/jobs.sqlite"))
    parser.add_argument("--account", action="append", default=[])
    parser.add_argument("--region", action="append", default=[])
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args(argv)
    providers = args.providers
    if args.include_provider:
        from atsuite.pipeline import PROVIDERS
        providers = (providers if providers is not None else [name for name in PROVIDERS if name != "mcp_gateway"]) + args.include_provider
    from atsuite.pipeline import PROVIDERS, provider_cloud_name
    from atsuite.recommender import service
    providers = providers or [name for name in PROVIDERS if name != 'mcp_gateway']
    if args.cloud: providers = [p for p in providers if provider_cloud_name(p) in args.cloud]
    if args.family: providers = [p for p in providers if PROVIDERS[p].family in args.family]
    if args.runs < 3 or args.warmup_runs < 0: parser.error('runs must be >= 3 and warmup-runs nonnegative')
    if args.weights or args.latency_stat != 'p95' or args.keep_resources or args.fx_config:
        parser.error('legacy custom weights, non-P95 latency, keep-resources and bare FX are not verified contracts; use a frozen experiment spec')
    objective = {'type': {'cost':'min_cost','latency':'min_latency','balanced':'balanced','reliability':'min_latency'}.get(args.objective,'balanced')}
    if args.max_p95_ms is not None: objective['max_p95_workflow_ms'] = args.max_p95_ms
    if args.max_failure_rate is not None: objective['min_success_rate'] = 1-args.max_failure_rate
    if args.max_cost_usd is not None: objective['max_cost_per_workflow'] = args.max_cost_usd
    if args.max_memory_mb is not None: objective['max_memory_mb'] = args.max_memory_mb
    try:
        spec=ExperimentSpec.from_dict({'benchmark':{'config':str(args.config)},'providers':providers,
            'regions':dict(item.split('=',1) for item in args.region),'objective':objective,
            'protocol':{'confirmation_runs':args.runs,'warmup_runs':args.warmup_runs},
            'budget':{'max_estimated_spend':args.max_budget,'max_wall_time_seconds':args.max_wall_time}})
        output=args.output_dir/'plan.json'
        result=service.plan(spec,output)
        if args.approve_budget:
            argv_run=['run','--plan',str(output),'--approve-budget','--store',str(args.store)]
            for account in args.account: argv_run.extend(['--account',account])
            if args.wait: argv_run.append('--wait')
            return _new_cli(argv_run)
        print(json.dumps(result,ensure_ascii=False,default=str) if args.json else f'Plan frozen: {output}; execution requires --approve-budget and --max-budget')
        return 0
    except (OSError, ValueError) as exc: parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
