from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class DatasetRow:
    workload_fingerprint: str
    candidate_id: str
    scenario_id: str
    observed_at: str
    features: dict[str, float]
    targets: dict[str, float]
    quality: str = "available"
    source_ref: str = ""
    workload_family: str = ""
    trial_ids: tuple[str, ...] = ()
    measurement_source: str = "unverified"
    phase: str = "exploration"
    observed_spend: str | None = None


class Dataset:
    def __init__(self, rows: Iterable[DatasetRow] = ()):
        self.rows = list(rows)

    def append(self, row: DatasetRow) -> None:
        self.rows.append(row)

    def write_jsonl(self, path: str | Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("".join(json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n" for row in self.rows), encoding="utf-8")
        return output

    @classmethod
    def read_jsonl(cls, path: str | Path) -> "Dataset":
        rows = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(DatasetRow(**json.loads(line)))
        return cls(rows)


    @classmethod
    def load(cls, path: str | Path) -> "Dataset":
        source = Path(path)
        if source.suffix == ".json":
            payload = json.loads(source.read_text(encoding="utf-8"))
            data_path = payload.get("data_path") if isinstance(payload, dict) else None
            if data_path:
                target = Path(data_path)
                if not target.is_absolute():
                    target = source.parent / target
                return cls.read_jsonl(target)
        return cls.read_jsonl(source)

    @classmethod
    def from_experiment_root(cls, root: str | Path) -> "Dataset":
        base = Path(root)
        if base.is_file():
            return cls.load(base)
        rows = []
        seen = set()
        for path in sorted(base.rglob("*.dataset.jsonl")) + sorted(base.rglob("dataset.jsonl")):
            if path in seen:
                continue
            seen.add(path)
            rows.extend(cls.read_jsonl(path).rows)
        for path in sorted(base.rglob("prediction-row.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows.append(DatasetRow(**payload))
        return cls(rows)

    def split(self, *, test_workloads=None, cutoff=None, test_families=None):
        # Entire families are held out if any member crosses the time boundary.
        holdout_groups = set()
        for row in self.rows:
            group = row.workload_family or row.workload_fingerprint
            if (test_workloads and row.workload_fingerprint in test_workloads) or (test_families and group in test_families) or (cutoff and row.observed_at >= cutoff):
                holdout_groups.add(group)
        train, test = [], []
        trial_groups = {}
        for row in self.rows:
            group = row.workload_family or row.workload_fingerprint
            for trial in row.trial_ids:
                if trial in trial_groups and trial_groups[trial] != group:
                    raise ValueError('duplicate trial crosses workload families')
                trial_groups[trial] = group
            (test if group in holdout_groups else train).append(row)
        return Dataset(train), Dataset(test)

    def qualified_history(self):
        return Dataset(row for row in self.rows if row.quality == 'available' and row.source_ref and row.measurement_source == 'platform_measured' and row.phase != 'confirmation')

    def coverage(self) -> dict[str, int]:
        return {"rows": len(self.rows), "workloads": len({row.workload_fingerprint for row in self.rows}), "candidates": len({row.candidate_id for row in self.rows}), "scenarios": len({row.scenario_id for row in self.rows})}
