from __future__ import annotations

from collections import defaultdict

from atsuite.prediction.dataset import Dataset
from atsuite.prediction.predictor import Predictor


def evaluate(predictor: Predictor, dataset: Dataset) -> dict:
    errors: dict[str, list[float]] = defaultdict(list)
    predictions = 0
    for row in dataset.rows:
        if predictor.model is None:
            continue
        predictions += 1
        for target, coefficients in predictor.model.coefficients.items():
            value = max(0.0, coefficients.get("intercept", 0.0) + sum(
                coefficients.get(name, 0.0) * row.features.get(name, 0.0) for name in predictor.model.feature_names
            ))
            if target in row.targets:
                errors[target].append(abs(value - row.targets[target]))
    per_target = {name: {"samples": len(values), "mae": sum(values) / len(values)} for name, values in sorted(errors.items()) if values}
    all_errors = [value for values in errors.values() for value in values]
    return {"rows": len(dataset.rows), "predictions": predictions, "per_target": per_target,
            "mae": sum(all_errors) / len(all_errors) if all_errors else None,
            "status": "evaluated" if all_errors else "insufficient_evidence"}
