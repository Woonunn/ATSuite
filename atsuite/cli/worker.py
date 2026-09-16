from __future__ import annotations

import argparse
import json
from pathlib import Path

from atsuite.execution.jobs import ExperimentJob
from atsuite.execution.worker import load_handlers
from atsuite.execution.store import JobStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one ATSuite recommender job in an external worker process")
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--handlers", required=True, help="Python handler factory as module:factory")
    args = parser.parse_args(argv)
    store = JobStore(args.store)
    try:
        handlers = load_handlers(args.handlers, store, args.experiment)
        result = ExperimentJob(store, args.experiment, handlers).run()
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
