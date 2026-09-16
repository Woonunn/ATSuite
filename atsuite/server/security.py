from __future__ import annotations

import hashlib
import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath


MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 2_000
MAX_COMPRESSION_RATIO = 200


def safe_extract_zip(archive: str | Path, destination: str | Path) -> dict[str, object]:
    source = Path(archive)
    target = Path(destination).resolve()
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("benchmark archive exceeds compressed size limit")
    target.mkdir(parents=True, exist_ok=True)
    total = 0
    files = 0
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    with zipfile.ZipFile(source) as bundle:
        infos = bundle.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise ValueError("benchmark archive exceeds file count limit")
        for info in infos:
            name = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if info.flag_bits & 0x1:
                raise ValueError("encrypted benchmark archives are not accepted")
            if name.is_absolute() or not name.parts or any(part in {"", ".", ".."} for part in name.parts):
                raise ValueError(f"unsafe archive path: {info.filename}")
            if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
                raise ValueError(f"archive entry type is not allowed: {info.filename}")
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("benchmark archive exceeds extracted size limit")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise ValueError(f"archive entry compression ratio is too high: {info.filename}")
            output = (target / Path(*name.parts)).resolve()
            if output != target and target not in output.parents:
                raise ValueError(f"unsafe archive path: {info.filename}")
            if info.is_dir():
                output.mkdir(parents=True, exist_ok=True)
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as reader, output.open("wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            files += 1
    configs = sorted(str(path.relative_to(target)) for path in target.rglob("*.json") if "config" in path.parts)
    return {"sha256": digest.hexdigest(), "file_count": files, "extracted_bytes": total, "config_files": configs}
