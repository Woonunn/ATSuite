from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from atsuite.prediction.features import make_features, FEATURE_VERSION
from atsuite.prediction.train import ModelArtifact


@dataclass(frozen=True)
class Prediction:
    values: dict[str, float]
    quality: str
    model_id: str
    feature_version: str = "2"
    ood: bool = False
    reason: str = ""


class BaselinePredictor:
    model_id = "baseline-median"

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def predict(self, target_names=("latency_ms", "memory_mb", "failure_risk")) -> Prediction:
        values = {}
        for target in target_names:
            samples = [float(row.targets[target]) for row in self.rows if target in row.targets]
            if not samples:
                return Prediction({}, "missing", self.model_id, reason=f"no training rows for {target}")
            values[target] = sorted(samples)[len(samples) // 2]
        return Prediction(values, "estimated", self.model_id)


class Predictor:
    def __init__(self, model: ModelArtifact | None = None, *, baseline: BaselinePredictor | None = None):
        self.model = model
        self.baseline = baseline or BaselinePredictor()

    def predict(self, workload: Mapping[str, Any], candidate: Any, *, pilot: Mapping[str, Any] | None = None) -> Prediction:
        if self.model is None:
            return self.baseline.predict()
        if self.model.feature_version != FEATURE_VERSION:
            return Prediction({}, "missing", self.model.model_id, ood=True, reason="feature_version_mismatch")
        features = make_features(workload, candidate, pilot=pilot)
        values = {}
        for target, coefficients in self.model.coefficients.items():
            values[target] = max(0.0, coefficients.get("intercept", 0.0) + sum(coefficients.get(name, 0.0) * features.get(name, 0.0) / self.model.scales.get(name, 1.0) for name in self.model.feature_names))
        for target in ("failure_risk", "failure_rate"):
            if target in values:
                values[target] = min(1.0, values[target])
        unknown = sum(1 for name in self.model.feature_names if name not in features)
        outside = []
        for name, (lower, upper) in self.model.feature_bounds.items():
            value = features.get(name)
            margin = max(abs(upper - lower) * .2, 1e-9)
            if value is not None and not lower - margin <= value <= upper + margin:
                outside.append(name)
        unsupported_categories = any(value == 1 for name,value in features.items() if name.endswith(".unknown") and name in self.model.feature_names)
        ood = unknown > len(self.model.feature_names) // 2 or bool(outside) or unsupported_categories
        reason = "feature coverage is outside training support" if ood else ""
        return Prediction(values, "predicted", self.model.model_id, ood=ood, reason=reason)
