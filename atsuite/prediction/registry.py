from __future__ import annotations

import json
import re
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from atsuite.prediction.train import ModelArtifact


class ModelRegistry:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, model: ModelArtifact) -> Path:
        output=self._path(model.model_id)
        content=json.dumps(asdict(model),indent=2,ensure_ascii=False,sort_keys=True,allow_nan=False)+'\n'
        fd,temporary=tempfile.mkstemp(dir=self.root,prefix='.model-')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(content);stream.flush();os.fsync(stream.fileno())
            try:os.link(temporary,output)
            except FileExistsError:
                if output.is_symlink() or output.read_text(encoding='utf-8')!=content:
                    raise ValueError('immutable_model_version_collision')
        finally:os.unlink(temporary)
        return output

    def _path(self,model_id):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',model_id):raise ValueError('invalid_model_id')
        path=self.root/f'{model_id}.json'
        if path.is_symlink():raise ValueError('invalid_model_path')
        return path

    def load(self, model_id: str) -> ModelArtifact:
        payload = json.loads(self._path(model_id).read_text(encoding="utf-8"))
        payload["feature_names"] = tuple(payload["feature_names"])
        payload["target_names"] = tuple(payload["target_names"])
        return ModelArtifact(**payload)
