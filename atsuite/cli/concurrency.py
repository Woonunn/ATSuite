import argparse
import math
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

from atsuite.con_invoker import run_trace
from atsuite.utils import _cleanup_scaling_configs, _create_scaling_configs
from atsuite.pipeline import resolve_benchmark, CliOverrides
from atsuite.state_snapshot import load_state_snapshot_bundle
from atsuite.uid_strategy import build_request_uid


RESULT_COLUMNS = [
    "index",
    "uid",
    "e2e_time_ms",
    "compute_time_ms",
    "response_time_ms",
    "idle_time_ms",
    "total_price",
    "precise_price",
    "total_cpu_price",
    "total_memory_price",
    "cold_starts",
    "avg_memory_mb",
    "vcpu_hours",
    "memory_gb_hours",
]


def build_unified_result_path(result_dir: Path, provider: str, timestamp: str | None = None) -> Path:
    ts = timestamp or uuid.uuid4().hex
    return result_dir / f"result_{provider}_{ts}.xlsx"


def save_unified_results(
    result_dir: Path,
    provider: str,
    results: List[Dict],
    timestamp: str | None = None,
) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    if results:
        df = pd.DataFrame(results)
    else:
        df = pd.DataFrame(columns=RESULT_COLUMNS)
    for column in RESULT_COLUMNS:
        if column not in df.columns:
            df[column] = None
    df = df[RESULT_COLUMNS]
    
    p99_row = {"index": "p99", "uid": ""}
    for col in RESULT_COLUMNS:
        if col in ("index", "uid"):
            continue
        values = [r[col] for r in results if isinstance(r.get(col), (int, float)) and math.isfinite(r[col])]
        if values:
            sorted_vals = sorted(values)
            p99_row[col] = sorted_vals[int(len(sorted_vals) * 0.99)]
        else:
            p99_row[col] = None
    
    df = pd.concat([df, pd.DataFrame([p99_row])], ignore_index=True)
    
    output_path = build_unified_result_path(result_dir, provider, timestamp=timestamp)
    coverage = [{"metric": column, "measured": sum(isinstance(row.get(column), (int, float)) and math.isfinite(row[column]) for row in results),
                 "requests": len(results)} for column in RESULT_COLUMNS if column not in {"index", "uid"}]
    with pd.ExcelWriter(output_path) as writer:
        df.to_excel(writer, index=False, sheet_name="results")
        pd.DataFrame(coverage).to_excel(writer, index=False, sheet_name="coverage")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Concurrently invoke run_trace to simulate multiple users.")
    parser.add_argument("--config", required=True, help="Path to benchmark config JSON.")
    parser.add_argument("--url-map", required=True, help="Path to JSON mapping node name to URL.")
    parser.add_argument(
        "--provider",
        required=True,
        choices=["ali_fc", "aws_lambda", "gcp_faas", "ali_agentrun", "aws_agentcore", "gcp_mcp", "mcp_gateway"],
        help="Provider name",
    )
    parser.add_argument("--concurrency", type=int, default=1, help="Number of concurrent requests (default: 1).")
    parser.add_argument("--uid-prefix", default="user", help="Prefix for user IDs (default: 'user'). UIDs will be {prefix}_{index}.")
    parser.add_argument(
        "--uid-mode",
        default="random",
        choices=["random", "index", "fixed"],
        help="UID generation mode: random={prefix}_{i}_{uuid8}, index={prefix}_{i}, fixed={uid-fixed}.",
    )
    parser.add_argument(
        "--uid-fixed",
        default=None,
        help="Exact UID used when --uid-mode fixed.",
    )
    parser.add_argument("--skip-sleep", action="store_true", default=False, help="Skip sleep between nodes.")
    parser.add_argument("--max-workers", type=int, default=None, help="Maximum number of concurrent ready nodes to execute.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility (default: 42).")
    parser.add_argument("--llm-time-mean", type=float, default=1.0, help="Mean of normal distribution for LLM time scale (default: 1.0).")
    parser.add_argument("--llm-time-std", type=float, default=0.2, help="Standard deviation of normal distribution for LLM time scale (default: 0.2).")
    parser.add_argument("--enable-analyzer", action="store_true", default=False, help="Enable analyzer to get real compute time from cloud logs (slower but more accurate).")
    parser.add_argument(
        "--state-snapshot",
        default=None,
        help="Optional path to state snapshot JSON (aws_agentcore injects in-request; aws_lambda pre-seeds S3 per UID).",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    url_map_path = Path(args.url_map).resolve()
    concurrency = args.concurrency
    uid_prefix = args.uid_prefix
    uid_mode = args.uid_mode
    uid_fixed = args.uid_fixed
    provider = args.provider
    state_snapshot_bundle = load_state_snapshot_bundle(Path(args.state_snapshot).resolve()) if args.state_snapshot else None
    if uid_mode == "fixed" and not uid_fixed:
        parser.error("--uid-fixed is required when --uid-mode fixed")

    np.random.seed(args.seed)
    random.seed(args.seed)
    llm_time_scales = np.random.normal(args.llm_time_mean, args.llm_time_std, concurrency)
    llm_time_scales = np.clip(llm_time_scales, 0.1, 3.0)

    resolved = resolve_benchmark(config_path, provider, CliOverrides())
    # if resolved.family == "mcp_serverless":
    #     for target in resolved.targets.values():
    #         _create_scaling_configs(provider, f"{target.name}-mcp", concurrency)
    #     if provider == "ali_agentrun":
    #         print("[concurrent_invoker] Waiting 30s for ali_agentrun...")
    #         time.sleep(30)

    print(f"[concurrent_invoker] Starting {concurrency} concurrent requests...")
    print(f"[concurrent_invoker] Random seed: {args.seed}, LLM time scale: mean={args.llm_time_mean}, std={args.llm_time_std}")
    print(f"[concurrent_invoker] Generated scales: min={llm_time_scales.min():.3f}, max={llm_time_scales.max():.3f}, mean={llm_time_scales.mean():.3f}")
    
    start_time = time.time()
    
    results: list = []
    failed_results: list = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {}

        for i in range(concurrency):
            uid = build_request_uid(
                uid_prefix=uid_prefix,
                index=i,
                uid_mode=uid_mode,
                uid_fixed=uid_fixed,
                uuid_hex=uuid.uuid4().hex,
            )
            llm_time_scale = float(llm_time_scales[i])

            future = executor.submit(
                run_trace,
                config_path,
                url_map_path,
                uid,
                provider=provider,
                skip_sleep=args.skip_sleep,
                max_workers=args.max_workers,
                llm_time_scale=llm_time_scale,
                skip_scaling_config=True,
                skip_analyzer=not args.enable_analyzer,
                state_snapshot_bundle=state_snapshot_bundle,
            )

            futures[future] = (uid, llm_time_scale, i)
            print(f"[concurrent_invoker] Submitted request {i+1}/{concurrency}, uid={uid}, llm_time_scale={llm_time_scale:.3f}")

        completed = 0
        for future in as_completed(futures):
            uid, llm_time_scale, idx = futures[future]
            completed += 1
            try:
                result = future.result()
                run_info = result.get("run", {})
                summary = result.get("summary", {})
                results.append({
                    "index": idx + 1,
                    "uid": run_info.get("uid", uid),
                    "e2e_time_ms": summary.get("run_user_e2e_ms"),
                    "compute_time_ms": summary.get("total_compute_time_ms"),
                    "response_time_ms": summary.get("total_app_e2e_ms"),
                    "idle_time_ms": summary.get("total_idle_time_ms"),
                    "total_price": summary.get("total_price"),
                    "precise_price": summary.get("total_price"),
                    "total_cpu_price": summary.get("total_cpu_price"),
                    "total_memory_price": summary.get("total_memory_price"),
                    "cold_starts": summary.get("cold_start_total"),
                    "avg_memory_mb": summary.get("avg_memory_usage_mb"),
                    "vcpu_hours": summary.get("vcpu_hours"),
                    "memory_gb_hours": summary.get("memory_gb_hours"),
                })
                print(
                    f"[concurrent_invoker] Completed {completed}/{concurrency}, "
                    f"uid={uid}, scale={llm_time_scale:.3f}, "
                    f"e2e={summary.get('run_user_e2e_ms')}ms"
                )
            except Exception as e:
                failed_results.append({
                    "index": idx + 1,
                    "uid": uid,
                    "llm_time_scale": llm_time_scale,
                    "error": str(e),
                })
                print(f"[concurrent_invoker] Failed {completed}/{concurrency}, uid={uid}, scale={llm_time_scale:.3f}: {e}")

    elapsed = time.time() - start_time
    print(f"[concurrent_invoker] All {concurrency} requests finished in {elapsed:.2f}s")
    
    con_result_dir = Path("con_result")
    con_result_dir.mkdir(exist_ok=True)
    
    results.sort(key=lambda x: x["index"])
    
    if results:
        xlsx_file = save_unified_results(con_result_dir, provider, results)
        print(f"[concurrent_invoker] Results saved to {xlsx_file}")
        
        print("[concurrent_invoker] Aggregated measured results:")
        for label, key in (("E2E time", "e2e_time_ms"), ("Compute time", "compute_time_ms"), ("Estimated price", "total_price")):
            values = sorted(row[key] for row in results if isinstance(row.get(key), (int, float)) and math.isfinite(row[key]))
            if values:
                print(f"  {label}: measured={len(values)}/{len(results)}, min={values[0]:.6g}, max={values[-1]:.6g}, "
                      f"mean={sum(values)/len(values):.6g}, p99={values[int(len(values)*.99)]:.6g}")
            else:
                print(f"  {label}: unavailable (measured=0/{len(results)})")

    if failed_results:
        df_failed = pd.DataFrame(failed_results)
        timestamp = uuid.uuid4().hex
        xlsx_failed = con_result_dir / f"failed_{provider}_{timestamp}.xlsx"
        df_failed.to_excel(xlsx_failed, index=False, sheet_name="failed")
        print(f"[concurrent_invoker] Failed results saved to {xlsx_failed}")

    # if resolved.family == "mcp_serverless":
    #     for target in resolved.targets.values():
    #         _cleanup_scaling_configs(provider, f"{target.name}-mcp")


if __name__ == "__main__":
    main()
