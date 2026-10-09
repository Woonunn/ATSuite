from __future__ import annotations

import json
import os
import tempfile
import re
from pathlib import Path
from typing import Any

from atsuite.recommender.identity import digest


class SnapshotStore:
    """Immutable JSON snapshots with atomic writes and explicit IDs."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, payload: dict[str, Any], *, snapshot_id: str | None = None) -> str:
        value = dict(payload)
        identifier = snapshot_id or value.get("snapshot_id") or "snapshot-" + digest(value, length=24)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",str(identifier)):
            raise ValueError("invalid snapshot identifier")
        value["snapshot_id"] = identifier
        path = self.root / f"{identifier}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != value:
                raise ValueError(f"immutable snapshot collision: {identifier}")
            return identifier
        fd, temp_name = tempfile.mkstemp(prefix=f".{identifier}.", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            try:
                os.link(temp_name,path)
            except FileExistsError:
                if json.loads(path.read_text(encoding="utf-8")) != value:
                    raise ValueError(f"immutable snapshot collision: {identifier}")
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return identifier

    def get(self, snapshot_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",str(snapshot_id)):
            raise ValueError("invalid snapshot identifier")
        return json.loads((self.root / f"{snapshot_id}.json").read_text(encoding="utf-8"))
