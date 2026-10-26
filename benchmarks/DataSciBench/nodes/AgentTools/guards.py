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
