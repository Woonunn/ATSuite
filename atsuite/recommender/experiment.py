from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from atsuite.analyzer import Analyzer
from atsuite.cli.build_docker_images import build_resolved_targets
from atsuite.deploy import deploy
from atsuite.invoker import run_trace
from atsuite.pipeline import read_url_map, resolve_benchmark
from atsuite.recommender.metrics import aggregate_runs
from atsuite.recommender.normalize import CurrencyNormalizer


class CandidateLifecycle:
    def __init__(self, candidate, output_dir: Path):
        self.candidate = candidate
        self.output_dir = output_dir
        self.resolved = resolve_benchmark(candidate.config_path, candidate.provider)
        self.url_map_path = None

    def build(self):
        # This existing function is already a library API, independent of parsing.
        build_resolved_targets(self.candidate.config_path, self.candidate.provider, str(self.output_dir / "build"))

    def deploy(self):
        path = deploy(self.candidate.config_path, self.candidate.provider)
        if path is None:
            raise RuntimeError("Deployment returned no endpoint map")
        payload = read_url_map(path)
        self.url_map_path = self.output_dir / "url-map.json"
        self.url_map_path.write_text(json.dumps(payload, indent=2) + "\n")
        missing = []
        for name in self.resolved.targets:
            entry = payload["targets"].get(name)
            endpoint = entry.get("endpoint") or entry.get("url") if isinstance(entry, dict) else entry
            if not isinstance(endpoint, str) or not endpoint.strip():
                missing.append(name)
        if missing:
            raise RuntimeError(f"Deployment returned no endpoints for: {', '.join(missing)}")

    def run(self, uid: str, phase: str) -> dict:
        run_dir = self.output_dir / uid
        run_dir.mkdir(parents=True, exist_ok=True)
        resolved = self.resolved
        analyzer = Analyzer(
            self.candidate.config_path, self.candidate.provider,
            observability_provider=resolved.provider.observability_provider,
            family=resolved.family, bench_name=resolved.bench_name, trace_name=resolved.trace_path.stem,
            targets={name: {"family": target.family, "runtime": target.runtime.to_dict(),
                            "nodes": list(target.node_names), "trace_names": list(target.trace_names)}
                     for name, target in resolved.targets.items()},
            endpoint_map=read_url_map(self.url_map_path), output_dir=run_dir,
        )
        record = {"uid": uid, "phase": phase, "status": "success", "summary": {}, "diagnostics": []}
        try:
            replay = run_trace(self.candidate.config_path, self.url_map_path, uid,
                               provider=self.candidate.provider, analyzer=analyzer, output_dir=run_dir, raise_on_error=False)
            record.update(status=replay["status"], summary=replay["summary"])
            if replay.get("error"):
                record["error"] = replay["error"]
                record["diagnostics"].append({"kind": "run_failed" if phase == "measured" else "warmup_failed", "error": replay["error"]})
            for kind in ("cleanup", "analysis"):
                if replay.get(f"{kind}_error"):
                    record["diagnostics"].append({"kind": f"{kind}_failed", "error": replay[f"{kind}_error"]})
        except (Exception, SystemExit) as exc:
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["diagnostics"].append({"kind": "run_failed" if phase == "measured" else "warmup_failed", "error": record["error"]})
        report = analyzer.get_report()
        if report is None:
            record["diagnostics"].append({"kind": "analysis_failed", "reason": "No AnalysisReport produced"})
        else:
            record.update(summary=report.summary, report_path=report.report_path,
                          evidence_path=report.evidence_path, trace_path=report.trace_path,
                          events_path=analyzer.events_path)
            record["diagnostics"].extend(report.diagnostics)
            if any(invocation["status"] != "ok" for invocation in report.invocations):
                record["status"] = "failed"
                record["diagnostics"].append({"kind": "run_failed", "reason": "Tool invocation failed"})
        if any(diagnostic["kind"] == "cleanup_failed" for diagnostic in record["diagnostics"]):
            availability = record["summary"].setdefault("metric_availability", {})
            if availability.get("total_price") == "available":
                availability["total_price"] = "partial"
        record["events_path"] = analyzer.events_path
        for key, value in list(record["summary"].items()):
            if isinstance(value, float) and not math.isfinite(value):
                record["summary"][key] = None
                record["summary"].setdefault("metric_availability", {})[key] = "unavailable"
                record["diagnostics"].append({"kind": "metric_unavailable", "metric": key, "reason": "Non-finite Analyzer value; see raw report"})
        (run_dir / "run.json").write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
        return record

    def cleanup(self) -> str:
        # ponytail: deployers expose no resource deletion API; implement here
        # when owned-resource deletion is available for these deployments.
        return "unsupported"


class ExperimentRunner:
    def __init__(self, *, output_dir: Path, fx_rates=None, keep_resources=False,
                 lifecycle_factory=CandidateLifecycle, progress=None):
        self.output_dir = Path(output_dir).resolve()
        self.normalizer = CurrencyNormalizer(fx_rates)
        self.keep_resources = keep_resources
        self.lifecycle_factory = lifecycle_factory
        self.progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))

    def run_candidate(self, candidate, *, runs: int, warmup_runs: int, uid_prefix: str):
        if runs < 3 or warmup_runs < 0:
            raise ValueError("At least 3 measured runs and nonnegative warmup_runs are required")
        directory = self.output_dir / candidate.id
        directory.mkdir(parents=True, exist_ok=True)
        records, diagnostics = [], []
        lifecycle = None
        stage, status, cleanup_status = "validate", "success", "not_needed"
        deployment_attempted = False
        try:
            if not candidate.supported:
                raise ValueError(candidate.unsupported_reason)
            lifecycle = self.lifecycle_factory(candidate, directory)
            stage = "build"
            self.progress(f"{candidate.id}: build")
            lifecycle.build()
            stage = "deploy"
            deployment_attempted = True
            self.progress(f"{candidate.id}: deploy")
            lifecycle.deploy()
            for phase, count in (("warmup", warmup_runs), ("measured", runs)):
                stage = phase
                for index in range(count):
                    uid = f"{uid_prefix}-{candidate.id}-{'run' if phase == 'measured' else 'warmup'}-{index}"
                    try:
                        record = lifecycle.run(uid, phase)
                    except (Exception, SystemExit) as exc:
                        record = {"uid": uid, "phase": phase, "status": "failed", "summary": {},
                                  "error": f"{type(exc).__name__}: {exc}",
                                  "diagnostics": [{"kind": "run_failed" if phase == "measured" else "warmup_failed", "error": str(exc)}]}
                    records.append(record)
                    (directory / "runs.json").write_text(json.dumps(records, indent=2, allow_nan=False) + "\n")
                    self.progress(f"{candidate.id}: {phase} {index + 1}/{count} {record['status']}")
        except (Exception, SystemExit) as exc:
            status = f"{stage}_failed"
            diagnostics.append({"kind": status, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            if lifecycle is not None and deployment_attempted:
                if self.keep_resources:
                    cleanup_status = "kept"
                else:
                    try:
                        cleanup_status = lifecycle.cleanup()
                    except (Exception, SystemExit) as exc:
                        cleanup_status = "failed"
                        diagnostics.append({"kind": "cleanup_failed", "error": str(exc)})
                if cleanup_status in {"unsupported", "failed"}:
                    message = f"{candidate.id}: cleanup {cleanup_status}; cloud resources may remain. Endpoint map: {directory / 'url-map.json'}"
                    self.progress(f"WARNING: {message}")
                    diagnostics.append({"kind": f"cleanup_{cleanup_status}", "reason": message})
        measurement = aggregate_runs(candidate, records, self.normalizer)
        if status != "success":
            measurement.status = status
        measurement.diagnostics.extend(diagnostics)
        measurement.cleanup_status = cleanup_status
        return measurement
