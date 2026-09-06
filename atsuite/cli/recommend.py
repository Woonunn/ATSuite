import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from atsuite.recommender.engine import RecommendationEngine
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


def main(argv=None):
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
    args = parser.parse_args(argv)
    providers = args.providers
    if args.include_provider:
        from atsuite.pipeline import PROVIDERS
        providers = (providers if providers is not None else [name for name in PROVIDERS if name != "mcp_gateway"]) + args.include_provider
    try:
        constraints = RecommendationConstraints(
            allowed_clouds=args.cloud, allowed_families=args.family, max_p95_latency_ms=args.max_p95_ms,
            max_cost_usd=args.max_cost_usd, max_failure_rate=args.max_failure_rate, max_memory_mb=args.max_memory_mb,
        )
        fx_rates = load_fx(args.fx_config)
        engine = RecommendationEngine(output_dir=args.output_dir, progress=lambda message: print(message, file=sys.stderr, flush=True))
        with execution_output_to_stderr():
            result = engine.recommend(args.config, providers=providers, runs=args.runs, warmup_runs=args.warmup_runs,
                                      objective=args.objective, weights=args.weights, constraints=constraints,
                                      fx_rates=fx_rates, latency_stat=args.latency_stat, keep_resources=args.keep_resources)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(recommendation_dict(result), indent=2, ensure_ascii=False, allow_nan=False, default=str))
    else:
        print(format_recommendation(result))
    return 0 if result.winner else 1


if __name__ == "__main__":
    raise SystemExit(main())
