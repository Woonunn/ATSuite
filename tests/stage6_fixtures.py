"""Explicit synthetic tariffs and audited execution bounds for local tests only."""

from atsuite.provider_model.analytical import RATES

TOOLS = [
    "python",
    "prepare_workspace",
    "execute_analysis",
    "merge_artifacts",
    "validate_result",
]


def tariff(provider="aws_lambda", floor=0.0):
    coupled = provider in {"aws_agentcore", "ali_agentrun"}
    return {
        "currency": "CNY" if provider.startswith("ali") else "USD",
        "source": ["synthetic tariff, not a cloud quote"],
        "date": "2026-10-30",
        "region": "test",
        "scope": "total_infrastructure",
        "cpu_basis": "active" if coupled else "allocated",
        "memory_basis": "session" if coupled else "active",
        "compute_in_pool_floor": False,
        "quantum_s": 0.001,
        "session_tail_s": 0,
        "storage_tail_s": 0,
        "free_disk_gb": 0.5,
        "transfer_fraction": 0,
        "calibration": {
            "reference_id": "test-reference",
            "tool_scale": 1,
            "evidence": ["synthetic common reference"],
        },
        "rates": {
            **dict.fromkeys(RATES, 0.0),
            "memory_gb_s": 0.00001,
            "invocation": 0.0000002,
            "read": 0.0000004,
            "write": 0.000005,
            "pool_per_s": floor,
        },
    }


def bounds():
    return {
        "timeout_s": 900,
        "max_cost": 1,
        "currency": "USD",
        "tools": TOOLS,
        "evidence": ["synthetic bounded executor"],
    }
