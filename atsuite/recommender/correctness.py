from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from atsuite.runtime import normalize_invocation_result


@dataclass(frozen=True)
class CorrectnessResult:
    passed: bool
    output_equal: bool | None
    state_equal: bool | None
    reason: str = ""
    details: dict[str, Any] | None = None


def normalize_result(value: Any) -> Any:
    return normalize_invocation_result(value)


def compare_outputs(expected: Any, actual: Any, comparator: Callable[[Any, Any], bool] | None = None) -> bool:
    if comparator:
        return bool(comparator(expected, actual))
    return normalize_result(expected) == normalize_result(actual)


def validate_trial(expected_output: Any = None, actual_output: Any = None, expected_state: Any = None, actual_state: Any = None, *, comparator: Callable[[Any, Any], bool] | None = None, state_comparator: Callable[[Any, Any], bool] | None = None, require_output: bool = True, require_state: bool = False) -> CorrectnessResult:
    output_equal = None if expected_output is None and actual_output is None else compare_outputs(expected_output, actual_output, comparator)
    state_equal = None if expected_state is None and actual_state is None else compare_outputs(expected_state, actual_state, state_comparator)
    if require_output and output_equal is not True:
        return CorrectnessResult(False, output_equal, state_equal, "output mismatch or missing")
    if require_state and state_equal is not True:
        return CorrectnessResult(False, output_equal, state_equal, "state mismatch or missing")
    return CorrectnessResult(True, output_equal, state_equal)


def validate_outputs(
    actual_outputs: Mapping[str, Mapping[str, Any]],
    expected_outputs: Mapping[str, Any],
    *,
    comparator: Callable[[Any, Any], bool] | None = None,
) -> dict[str, Any]:
    rows = {}
    passed = True
    for call_id in sorted(set(actual_outputs) | set(expected_outputs)):
        actual = actual_outputs.get(call_id, {}).get("output") if call_id in actual_outputs else None
        expected = expected_outputs.get(call_id)
        result = validate_trial(expected, actual, comparator=comparator)
        rows[call_id] = asdict(result)
        passed = passed and result.passed
    return {"status": "passed" if passed else "failed", "calls": rows}
