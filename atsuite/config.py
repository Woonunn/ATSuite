"""Load trusted local process configuration before constructing services."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / '.env'


def load_local_env() -> bool:
    """Existing process variables win; never discover files in workload paths.

    ATSUITE_ENV_FILE optionally selects a server/operator-owned file explicitly.
    PYTHON_DOTENV_DISABLED is honored by python-dotenv.
    """
    configured = os.environ.get('ATSUITE_ENV_FILE')
    path = Path(configured).expanduser() if configured else PROJECT_ENV_FILE
    return load_dotenv(dotenv_path=path, override=False, encoding='utf-8')
