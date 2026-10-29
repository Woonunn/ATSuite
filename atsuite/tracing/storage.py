"""Compressed trace storage with transparent reading of historical plain files."""

import gzip
import json
import shutil
from pathlib import Path
from typing import Any


def trace_path(path: Path) -> Path:
    """Prefer the current compressed snapshot over a historical plain copy."""
    compressed = path.with_name(path.name + ".gz")
    return compressed if compressed.is_file() else path


def read_text(path: Path) -> str:
    """Read a logical trace filename in either storage format."""
    actual = trace_path(path)
    if actual.suffix == ".gz":
        with gzip.open(actual, "rt", encoding="utf-8") as source:
            return source.read()
    return actual.read_text(encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    """Atomically replace a gzip JSON snapshot, preserving the last good file on failure."""
    data = (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    target = path.with_name(path.name + ".gz")
    temporary = target.with_name(target.name + ".tmp")
    try:
        temporary.write_bytes(gzip.compress(data, compresslevel=6, mtime=0))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def compress_log(path: Path) -> None:
    """Finalize an append-only log; retain the readable source until compression succeeds."""
    if not path.exists():
        return
    target = path.with_name(path.name + ".gz")
    temporary = target.with_name(target.name + ".tmp")
    try:
        with path.open("rb") as source, temporary.open("wb") as destination:
            with gzip.GzipFile(filename="", fileobj=destination, mode="wb", compresslevel=6, mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
        temporary.replace(target)
        path.unlink()
    finally:
        temporary.unlink(missing_ok=True)
