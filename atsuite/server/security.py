from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 2_000
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200

# File references are resolved relative to their owning JSON file, matching
# benchmark runtime semantics. Remote references require a separately installed
# server-approved data source and are never accepted from uploaded configuration.
_REFERENCE_KEYS = {'trace_file', 'dir', 'data_file', 'data_dir', 'init', 'init_script',
                   'oracle_file', 'oracle_path', 'oracle_ref', 'fx_ref', 'ledger_ref',
                   'usage_ledger', 'source_path', 'input_file', 'state_file', 'data_path',
                   'init_path', 'fx_path', 'ledger_path', 'state_snapshot_path'}


def confined_path(base: Path, reference: str, root: Path, *, must_exist=True) -> Path:
    decoded = unquote(reference)
    if '\\' in decoded or '\x00' in decoded or urlsplit(decoded).scheme or Path(decoded).is_absolute():
        raise ValueError('absolute or remote file reference is not allowed')
    value = (base / decoded).resolve()
    if value != root and root not in value.parents:
        raise ValueError('benchmark reference escapes authorized bundle')
    if must_exist and not value.exists():
        raise ValueError('benchmark reference does not exist')
    return value


def validate_bundle_references(root: str | Path) -> list[str]:
    root = Path(root).resolve()
    configs = []
    for path in sorted(root.rglob('*.json')):
        if path.is_symlink():
            raise ValueError('symlink references are not accepted')
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f'invalid JSON: {path.relative_to(root)}') from exc
        is_config = 'config' in path.relative_to(root).parts
        reference_base = path.parent
        if is_config:
            # ATSuite configs resolve ./nodes and ./trace from benchmark root.
            config_parent = next(parent for parent in path.parents if parent.name == 'config')
            reference_base = config_parent.parent
        if is_config:
            configs.append(str(path.relative_to(root)))
        def check(item):
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in _REFERENCE_KEYS and isinstance(value, str) and value:
                        confined_path(reference_base, value, root)
                    elif key in {'handler_module', 'worker_module', 'shell_command', 'python_import'}:
                        raise ValueError('uploaded execution commands or modules are not allowed')
                    elif key in {'data_url', 'source_url', 'endpoint', 'download_url'} and isinstance(value, str) and value:
                        raise ValueError('remote data sources require a server-authorized source ID')
                    check(value)
            elif isinstance(item, list):
                for value in item:
                    check(value)
        check(payload)
    return configs


def safe_extract_zip(archive: str | Path, destination: str | Path) -> dict[str, object]:
    source = Path(archive)
    target = Path(destination).absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('benchmark destination already exists')
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('benchmark archive exceeds compressed size limit')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.upload-', dir=target.parent))
    total = files = 0
    archive_digest = hashlib.sha256()
    with source.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            archive_digest.update(chunk)
    try:
        with zipfile.ZipFile(source) as bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise ValueError('benchmark archive exceeds file count limit')
            seen = set()
            for info in infos:
                decoded = unquote(info.filename)
                name = PurePosixPath(decoded)
                mode = info.external_attr >> 16
                if '\\' in decoded or '\x00' in decoded or name.is_absolute() or not name.parts or '..' in name.parts:
                    raise ValueError('unsafe archive path')
                normalized = str(name).casefold()
                if normalized in seen:
                    raise ValueError('duplicate or case-colliding archive entry')
                seen.add(normalized)
                if info.flag_bits & 1 or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ValueError('archive entry type is not allowed')
                total += info.file_size
                if total > MAX_EXTRACTED_BYTES or info.file_size > MAX_FILE_BYTES:
                    raise ValueError('benchmark archive exceeds extracted size limit')
                if info.file_size and (not info.compress_size or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO):
                    raise ValueError('archive entry compression ratio is too high')
                output = staging / Path(*name.parts)
                if info.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                actual = 0
                with bundle.open(info) as reader, output.open('xb') as writer:
                    while chunk := reader.read(64 * 1024):
                        actual += len(chunk)
                        if actual > info.file_size or actual > MAX_FILE_BYTES:
                            raise ValueError('archive entry exceeds declared size')
                        writer.write(chunk)
                files += 1
        configs = validate_bundle_references(staging)
        if not configs:
            raise ValueError('benchmark package contains no config JSON under a config directory')
        os.rename(staging, target)
        return {'sha256': archive_digest.hexdigest(), 'file_count': files,
                'extracted_bytes': total, 'config_files': configs}
    finally:
        if staging.exists():
            shutil.rmtree(staging)
