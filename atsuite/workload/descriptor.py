"""Small, arrival-time resource descriptors; no task IDs or observed DAGs."""

import hashlib
import json
import math
from pathlib import PurePath

from atsuite.workload.profile import number


def describe(*, prompt="", files=(), tools=(), category=None, hints=None) -> dict:
    """Describe only supplied inputs and declared resource hints, never outcomes."""
    if not isinstance(prompt, str):
        raise ValueError("Prompt must be text")
    result = {
        "prompt_chars": len(prompt),
        "prompt_lines": len(prompt.splitlines()),
        "code_blocks": prompt.count("```"),
        "file_count": len(files),
        "input_bytes": sum(f["bytes"] for f in files if number(f.get("bytes"))),
        "unknown_file_sizes": sum(not number(f.get("bytes")) for f in files),
        "file_types": sorted({PurePath(f["name"]).suffix.lower() for f in files}),
        "tools": sorted(set(tools)),
        "category": category,
        "hints": dict(hints or {}),
    }
    validate_descriptor(result)
    return result


def validate_descriptor(value: dict) -> None:
    """Reject outcome features and malformed resource hints at the input boundary."""
    numeric = {
        "prompt_chars",
        "prompt_lines",
        "code_blocks",
        "file_count",
        "input_bytes",
        "unknown_file_sizes",
    }
    if set(value) != numeric | {"file_types", "tools", "category", "hints"}:
        raise ValueError("Descriptor must contain only arrival-time features")
    if any(not number(value[k]) for k in numeric):
        raise ValueError("Invalid descriptor feature")
    if value["category"] is not None and not isinstance(value["category"], str):
        raise ValueError("Invalid category")
    for field in ("tools", "file_types"):
        if not isinstance(value[field], list) or any(
            not isinstance(v, str) for v in value[field]
        ):
            raise ValueError("Invalid descriptor names")
    if not isinstance(value["hints"], dict) or any(
        k not in {"cpu_s", "memory_mb", "state_bytes"} or not number(v)
        for k, v in value["hints"].items()
    ):
        raise ValueError("Invalid resource hints")


def distance(left: dict, right: dict) -> float:
    """Log-scaled sizes plus tool/type overlap; one means substantially different."""
    parts = [
        abs(math.log2(1 + left[k]) - math.log2(1 + right[k])) / 4
        for k in ("prompt_chars", "file_count", "input_bytes")
    ]
    for key in ("file_types", "tools"):
        a, b = set(left[key]), set(right[key])
        parts.append(1 - len(a & b) / len(a | b) if a | b else 0)
    for key in set(left["hints"]) | set(right["hints"]):
        a, b = left["hints"].get(key), right["hints"].get(key)
        parts.append(
            abs(math.log2(1 + a) - math.log2(1 + b)) / 4
            if a is not None and b is not None
            else 1
        )
    if left["category"] and right["category"]:
        parts.append(float(left["category"] != right["category"]))
    return sum(parts) / len(parts)


def family_key(value: dict | None) -> str:
    """Stable resource-family label for offered-load accounting, independent of task."""
    if value is None:
        return "global"
    validate_descriptor(value)
    coarse = {k: value[k] for k in ("file_types", "tools", "category")}
    coarse.update(
        {
            k: int(math.log2(1 + value[k]) // 4)
            for k in ("input_bytes", "file_count", "prompt_chars")
        }
    )
    return hashlib.sha256(json.dumps(coarse, sort_keys=True).encode()).hexdigest()[:16]
