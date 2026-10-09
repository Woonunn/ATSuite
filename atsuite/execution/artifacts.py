from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from atsuite.recommender.contracts import BuildArtifact
from atsuite.recommender.identity import build_id, digest, file_digest


class ArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write_json(self, relative: str, payload: dict[str, Any]) -> Path:
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("artifact path escapes store")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)
                handle.write("\n")
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return path

    def read_json(self, relative: str) -> dict[str, Any]:
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("artifact path escapes store")
        return json.loads(path.read_text(encoding="utf-8"))


def create_build_artifact(*, repository: str, digest_value: str, platform: str, source_digest: str, dockerfile_digest: str, dependency_lock_digest: str = "", manifest_path: str = "", log_path: str = "") -> BuildArtifact:
    identifier = build_id(source_digest=source_digest, dependency_lock_digest=dependency_lock_digest, dockerfile_digest=dockerfile_digest,
                         topology=None, platform=platform)
    import re
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest_value):
        raise ValueError("build artifact digest must be a content digest")
    return BuildArtifact(identifier, repository, digest_value, platform, source_digest, dockerfile_digest, dependency_lock_digest, manifest_path, log_path)


def build_manifest(*, artifact: BuildArtifact, candidate_ids: list[str], source_files: list[str] | None = None) -> dict[str, Any]:
    return {"schema_version": 1, "build_id": artifact.build_id, "image": {"repository": artifact.repository, "digest": artifact.digest, "platform": artifact.platform},
            "source_digest": artifact.source_digest, "dockerfile_digest": artifact.dockerfile_digest,
            "dependency_lock_digest": artifact.dependency_lock_digest, "candidate_ids": sorted(set(candidate_ids)),
            "source_files": sorted(source_files or [])}
