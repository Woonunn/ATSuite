from __future__ import annotations

import argparse
import json
from pathlib import Path

from atsuite.prediction.dataset import Dataset


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export ATSuite predictor dataset metadata")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--experiments", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    dataset = Dataset.from_experiment_root(args.experiments)
    data_path = args.output.with_suffix(".jsonl")
    dataset.write_jsonl(data_path)
    manifest = {"schema_version": 1, "source": str(args.experiments), "data_path": data_path.name, "coverage": dataset.coverage()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
