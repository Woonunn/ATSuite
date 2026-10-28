import os
from pathlib import PurePosixPath
import re


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value):
        raise ValueError('Invalid identifier')
    return value


def relative_file(value):
    if not isinstance(value,str) or not value or len(value)>200 or '\\' in value:
        raise ValueError('Invalid artifact path')
    p=PurePosixPath(value)
    if not p.parts or p.as_posix()!=value or '\x00' in value or p.is_absolute() or '..' in p.parts or any(part.startswith('.') for part in p.parts):
        raise ValueError('Artifact path escapes output directory')
    return value


def safe_files(root,aliases=None):
    result=[];total=0
    for p in root.rglob('*'):
        if p.is_symlink() and aliases and aliases.get(p.relative_to(root).as_posix())==os.readlink(p):continue
        if p.is_symlink() or (not p.is_file() and not p.is_dir()):raise ValueError('Sandbox produced a link or special file')
        if p.is_file():
            total+=p.stat().st_size;result.append(p)
    if total>1024**3 or len(result)>256:raise ValueError('Sandbox output quota exceeded')
    return result
