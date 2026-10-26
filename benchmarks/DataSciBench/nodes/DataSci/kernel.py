"""Shared Python execution primitive. Call only in an isolated Tool worker."""
import os
import sys
import traceback
from pathlib import Path


def execute_code(code, namespace, output_path, filename='__session__.py'):
    if output_path is None:
        # Legacy FaaS streams diagnostics so timeout callers retain partial output.
        exec(compile(code.strip(), filename, 'exec'), namespace)
        return {'status':'ok','error':'','output':''}
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    status, error = 'ok', ''
    with Path(output_path).open('w+b') as capture:
        try:
            os.dup2(capture.fileno(), 1)
            os.dup2(capture.fileno(), 2)
            exec(compile(code.strip(), filename, 'exec'), namespace)
        except BaseException:
            status, error = 'error', traceback.format_exc()
            print(error, file=sys.stderr, end='')
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            for source, target in zip(saved, (1, 2)):
                os.dup2(source, target)
                os.close(source)
        capture.seek(0)
        output = capture.read().decode('utf-8', errors='replace')
    return {'status': status, 'error': error, 'output': output}
