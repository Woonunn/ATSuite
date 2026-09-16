from __future__ import annotations

import json
from pathlib import Path

from atsuite.prediction.train import ModelArtifact


class ModelRegistry:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, model: ModelArtifact) -> Path:
        return model.save(self.root / f"{model.model_id}.json")

    def load(self, model_id: str) -> ModelArtifact:
        payload = json.loads((self.root / f"{model_id}.json").read_text(encoding="utf-8"))
        payload["feature_names"] = tuple(payload["feature_names"])
        payload["target_names"] = tuple(payload["target_names"])
        return ModelArtifact(**payload)
