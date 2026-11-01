"""Fresh prompts through live LangGraph planning and the existing Docker sandbox."""

import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

from dotenv import load_dotenv

from atsuite.provider_model import ProviderModel
from atsuite.routing import OnlineRouter
from atsuite.tracing.storage import read_text
from atsuite.workload import WorkloadModel
from experiments.exp1.capture.capture_agent_traces import run
from experiments.exp1.tests.check_agent_lifecycle import ROOT, service

PROMPTS = {
    "human_15": (
        "Analyze sales.csv (columns Month, Year, Sales, Category; Month contains full English month names). "
        "Construct calendar dates from Year and Month with day=1, and deliver three charts. In bar_chart.png show annual total Sales "
        "with separated red bars and black edges. In line_chart.png show monthly totals by "
        "Category, using chronological month order, distinct colors, a legend, and dash-dot lines. "
        "In scatter_chart.png show individual Sales against calendar Time, colored by Category, "
        "with marker area proportional to Sales and alpha 0.5. Read and prepare the source once, "
        "then produce the three figures in independent branches sharing the prepared data."
    ),
    "csv_excel_34": (
        "Use data_pums_2000.csv to measure income inequality. Keep every individual with finite, "
        "nonnegative INCTOT (including repeated AGE/INCTOT rows), and save cleaned_data.csv. "
        "Sort those incomes once and retain the sorted array in shared state. From the shared "
        "array compute the Gini coefficient as one number in gini_coefficient.txt, and create "
        "lorenz_curve.png including the origin and equality line. The two final outputs may "
        "run in parallel after preparation; do not clip high incomes or substitute a sample."
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.image = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True
    ).strip()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    load_dotenv(ROOT / ".env", override=False)
    base = json.loads(
        (ROOT / "benchmarks/DataSciBench/config/langgraph_experiment.json").read_text()
    )
    base.update(max_iterations=2, session_timeout_s=600)
    base["model"].update(max_tokens=6000, retries=1)
    pool = {
        "provider": "local",
        "environment": args.image,
        "resources": {"cpu": 2, "memory_mb": 4096, "timeout_s": 180},
        "instances": 1,
        "sessions_per_instance": 1,
        "invocations_per_instance": 3,
    }
    bound = {
        "timeout_s": 600,
        "max_cost": 1,
        "currency": "USD",
        "tools": [
            "prepare_workspace",
            "execute_analysis",
            "merge_artifacts",
            "validate_result",
        ],
        "evidence": [
            "existing Docker confinement; 180s tool timeout, 4GB, 2 CPU, no network",
            "local-only infrastructure cap; LLM API fees excluded",
        ],
    }
    router = OnlineRouter(
        WorkloadModel(),
        ProviderModel(),
        {"sandbox": pool},
        {"sandbox": 1},
        default_pool="sandbox",
        execution_bounds={"sandbox": bound},
        budget=2,
        log_path=args.output / "routing.jsonl",
    )
    outcomes = []
    for task_id, prompt in PROMPTS.items():
        area = args.output / task_id
        with service("faas", area / "environment", args.image) as tool:
            try:
                result = asyncio.run(
                    run(
                        task_id,
                        {**base, "tool": tool},
                        area / "request",
                        prompt=prompt,
                        router=router,
                        routing_context={
                            "load_rps": 0,
                            "targets": {"sandbox": {"tool": tool}},
                        },
                    )
                )
            except Exception as exc:
                result = {"status": "failed", "error": str(exc)}
            decision = json.loads(read_text(area / "request/routing_decision.json"))
            result.update(
                task_id=task_id,
                prompt=prompt,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                selected=decision["selected"],
                history_count=decision["workload"]["sample_count"],
                reason=decision["reason"],
            )
            outcomes.append(result)
            print(task_id, result["status"], flush=True)
    report = {
        "scope": "Fresh prompts, live DeepSeek planning, real local Docker Tool execution; no cloud billing",
        "cases": outcomes,
        "all_successful": all(r["status"] == "complete" for r in outcomes),
        "workload_feedback_count": len(router.workload.samples),
        "image": args.image,
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    if not report["all_successful"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
