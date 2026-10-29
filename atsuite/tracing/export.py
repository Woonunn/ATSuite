"""Export spans or an ATG on demand: python -m atsuite.tracing.export DIRECTORY {spans,atg}."""

import argparse
import json
from pathlib import Path

from atsuite.tracing.core import export_atg
from atsuite.tracing.storage import read_text


def export_trace(directory: Path, format: str) -> Path:
    """Materialize one derived format from a compressed or historical canonical trace."""
    if format not in {"spans", "atg"}:
        raise ValueError("Expected spans or atg")
    trace = json.loads(read_text(directory / "trace.json"))
    if trace.get("schema_version") != 1:
        raise ValueError("Unsupported trace schema")
    path = directory / ("spans.jsonl" if format == "spans" else "trace.atg.json")
    if format == "atg":
        export_atg(trace, path)
    else:
        path.write_text(
            "".join(json.dumps(span, ensure_ascii=False, allow_nan=False) + "\n" for span in trace["spans"]),
            encoding="utf-8",
        )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("format", choices=("spans", "atg"))
    args = parser.parse_args()
    print(export_trace(args.directory, args.format))


if __name__ == "__main__":
    main()
