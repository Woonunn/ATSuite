from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Iterable, Optional

from atsuite.analysis.model import AnalysisReport, EvidenceRecord


class AnalysisExporter:
    def __init__(self, base_dir: str | Path = "results"):
        self.base_dir = Path(base_dir)

    def export(
        self,
        report: AnalysisReport,
        evidence: Iterable[EvidenceRecord],
        *,
        provider: str,
        benchmark: str,
        timestamp: Optional[str] = None,
    ) -> AnalysisReport:
        ts = timestamp or uuid.uuid4().hex
        output_dir = self.base_dir / provider / benchmark
        output_dir.mkdir(parents=True, exist_ok=True)

        evidence_path = output_dir / f"{ts}.evidence.jsonl"
        with evidence_path.open("w", encoding="utf-8") as handle:
            for record in evidence:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n")

        report.evidence_path = str(evidence_path)
        report_path = output_dir / f"{ts}.report.json"
        report.report_path = str(report_path)
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        return report


def print_summary(report: AnalysisReport) -> None:
    summary = report.summary
    print("\n" + "=" * 60)
    print("ATSUITE ANALYSIS V2")
    print("=" * 60)
    print(f"Provider: {report.run.get('provider')} ({report.run.get('observability_provider')})")
    print(f"Benchmark: {report.run.get('benchmark')} / {report.run.get('trace')}")
    for label, key, unit in (
        ("Run User E2E", "run_user_e2e_ms", "ms"), ("Total Compute", "total_compute_time_ms", "ms"),
        ("Total Initialize", "total_initialize_time_ms", "ms"), ("Total Idle", "total_idle_time_ms", "ms"),
        ("Total Client E2E", "total_client_e2e_ms", "ms"), ("Total App E2E", "total_app_e2e_ms", "ms"),
        ("Total Tool Exec", "total_tool_exec_ms", "ms"), ("Cold Starts", "cold_start_total", ""),
        ("Avg Memory", "avg_memory_usage_mb", "MB"), ("Estimated Price", "total_price", summary.get("currency", "USD")),
    ):
        value = summary.get(key)
        print(f"{label}: {'unavailable' if value is None else f'{value:.10g}{unit}'}")
    if report.diagnostics:
        print(f"Diagnostics: {len(report.diagnostics)} issue(s)")
    print(f"Report: {report.report_path}")
    print(f"Evidence: {report.evidence_path}")
    print(f"Trace: {report.trace_path}")
    print("=" * 60)
