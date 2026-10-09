from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from atsuite.prediction.dataset import Dataset


@dataclass(frozen=True)
class ModelArtifact:
    model_id: str
    model_type: str
    feature_names: tuple[str, ...]
    target_names: tuple[str, ...]
    coefficients: dict[str, dict[str, float]]
    means: dict[str, float]
    train_rows: int
    train_workloads: int
    feature_bounds: dict[str, tuple[float, float]] = field(default_factory=dict)
    version: str = "1"
    feature_version: str = "2"
    quality: str = "insufficient_real_data"
    training_data_digest: str = ""
    scales: dict[str, float] = field(default_factory=dict)
    training_workload_families: tuple[str, ...] = ()
    training_trial_ids: tuple[str, ...] = ()
    trained_through: str = ""

    def save(self, path: str | Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return output


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    # Gaussian elimination with ridge diagonal for a dependency-free model.
    n = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            continue
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        divisor = augmented[col][col]
        augmented[col] = [value / divisor for value in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            augmented[row] = [left - factor * right for left, right in zip(augmented[row], augmented[col])]
    return [row[-1] for row in augmented]


def train_model(dataset: Dataset, *, target_names: tuple[str, ...] = ("latency_ms", "memory_mb", "failure_risk"), model_id: str = "model-1") -> ModelArtifact:
    rows = [row for row in dataset.rows if row.quality == "available"]
    if not rows:
        raise ValueError("cannot train with no available rows")
    if any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) for row in rows for v in row.features.values()):
        raise ValueError("invalid training feature")
    feature_names = tuple(sorted({name for row in rows for name in row.features}))
    means = {name: sum(row.features.get(name, 0.0) for row in rows) / len(rows) for name in feature_names}
    bounds = {name: (min(float(row.features.get(name, 0.0)) for row in rows), max(float(row.features.get(name, 0.0)) for row in rows)) for name in feature_names}
    scales = {name: max(abs(bounds[name][0]), abs(bounds[name][1]), 1.0) for name in feature_names}
    regularization = 1e-6
    coefficients = {}
    trained_targets = []
    for target in target_names:
        target_rows = [row for row in rows if target in row.targets]
        if not target_rows:
            continue
        matrix = [[1.0] + [float(row.features.get(name, 0.0)) / scales[name] for name in feature_names] for row in target_rows]
        gram = [[sum(row[i] * row[j] for row in matrix) + (regularization if i == j else 0.0) for j in range(len(feature_names) + 1)] for i in range(len(feature_names) + 1)]
        values = [float(row.targets[target]) for row in target_rows]
        if any(not math.isfinite(value) or value < 0 or (target in {"failure_risk", "failure_rate"} and value > 1) for value in values):
            raise ValueError(f"invalid training target: {target}")
        rhs = [sum(row[i] * values[index] for index, row in enumerate(matrix)) for i in range(len(feature_names) + 1)]
        solution = _solve(gram, rhs)
        coefficients[target] = {"intercept": solution[0], **{name: solution[index + 1] for index, name in enumerate(feature_names)}}
        trained_targets.append(target)
    if not trained_targets:
        raise ValueError("training rows contain none of the requested targets")
    from atsuite.recommender.identity import digest
    return ModelArtifact(model_id, "linear_ridge", feature_names, tuple(trained_targets), coefficients, means,
                         len(rows), len({row.workload_fingerprint for row in rows}), bounds,
                         training_data_digest=digest([asdict(row) for row in rows], length=32), scales=scales,
                         training_workload_families=tuple(sorted({row.workload_family or row.workload_fingerprint for row in rows})),
                         training_trial_ids=tuple(sorted({t for row in rows for t in row.trial_ids})),
                         trained_through=max(row.observed_at for row in rows))
