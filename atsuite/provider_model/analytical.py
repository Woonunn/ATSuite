"""Resource-demand pricing with explicit, dated tariffs and disjoint pool charges."""

import math
from statistics import median

from atsuite.provider_model.observation import (
    BACKENDS,
    execution_context,
    validate_pool,
)
from atsuite.workload.model import distribution
from atsuite.workload.profile import number

RATES = (
    "cpu_s",
    "memory_gb_s",
    "invocation",
    "read",
    "write",
    "storage_gb_s",
    "transfer_gb",
    "disk_gb_s",
    "pool_per_s",
    "shared_per_s",
    "scale_event",
)


def validate_tariff(card: dict, provider: str) -> None:
    """Require explicit zero rates as well as billing units and price provenance."""
    if (
        card.get("currency") != BACKENDS[provider][2]
        or not all(card.get(k) for k in ("source", "date", "region", "scope"))
        or card.get("scope") != "total_infrastructure"
        or card.get("cpu_basis") not in {"active", "allocated"}
        or card.get("memory_basis") not in {"active", "session"}
        or not isinstance(card.get("compute_in_pool_floor"), bool)
        or not number(card.get("quantum_s"))
        or card["quantum_s"] == 0
        or any(not number(card.get("rates", {}).get(k)) for k in RATES)
    ):
        raise ValueError("Incomplete analytical tariff or billing provenance")
    for field in (
        "session_tail_s",
        "storage_tail_s",
        "free_disk_gb",
        "transfer_fraction",
    ):
        if not number(card.get(field)):
            raise ValueError("Explicit billing assumption required: " + field)
    if card["transfer_fraction"] > 1:
        raise ValueError("Transfer fraction must be between zero and one")
    calibration = card.get("calibration", {})
    if any(
        not number(v)
        for k, v in calibration.items()
        if k not in {"evidence", "reference_id"}
    ):
        raise ValueError("Invalid platform calibration")
    if "tool_scale" in calibration and (
        not isinstance(calibration.get("reference_id"), str)
        or not calibration["reference_id"]
    ):
        raise ValueError("A speed calibration requires its reference basis")
    if calibration and not calibration.get("evidence"):
        raise ValueError("Platform calibration needs evidence")


def calibration(card: dict, rows: list[dict], reference_id: str | None = None) -> dict:
    """Fit overheads from visible profiling rows, without fitting historical prices."""
    configured = card.get("calibration", {})
    reference_id = reference_id or configured.get("reference_id")
    result = {
        "tool_scale": 1.0,
        "init_s": 0.0,
        "sync_s_per_byte": 0.0,
        "sync_s_per_call": 0.0,
        **{
            k: v
            for k, v in configured.items()
            if k not in {"tool_scale", "billed_scale", "reference_id"}
        },
        "reference_id": reference_id,
    }
    declared = bool(
        reference_id
        and configured.get("reference_id") == reference_id
        and "tool_scale" in configured
    )
    if declared:
        result["tool_scale"] = configured["tool_scale"]
    ratios, inits, syncs, billed_ratios = [], [], [], []
    for row in rows:
        if not reference_id or row["profile"].get("reference_id") != reference_id:
            continue
        m, p = row["measurements"], row["profile"].get("metrics", {})
        if (
            number(m.get("tool_s"))
            and number(p.get("tool_elapsed_s"))
            and p["tool_elapsed_s"] > 0
        ):
            # Runtime execution_elapsed_ms includes restore/persist; price synchronization once.
            if number(m.get("state_sync_s")) and m["state_sync_s"] <= m["tool_s"]:
                ratios.append(
                    max(0, m["tool_s"] - m["state_sync_s"]) / p["tool_elapsed_s"]
                )
        if number(m.get("cold_start_s")):
            inits.append(m["cold_start_s"])
        if (
            number(m.get("billed_compute_s"))
            and number(p.get("tool_elapsed_s"))
            and p["tool_elapsed_s"] > 0
        ):
            billed_ratios.append(m["billed_compute_s"] / p["tool_elapsed_s"])
        calls = sum(row["profile"].get("workflow", {}).get("tool_calls", {}).values())
        peak = p.get("state_max_object_bytes")
        if number(m.get("state_sync_s")) and number(peak) and peak > 0 and calls:
            syncs.append(m["state_sync_s"] / (2 * peak * calls))
    for key, values in (
        ("tool_scale", ratios),
        ("init_s", inits),
        ("sync_s_per_byte", syncs),
        ("billed_scale", billed_ratios),
    ):
        if values:
            result[key] = median(values)
    result["sample_count"] = len(ratios)
    result["calibrated"] = declared or len(ratios) >= 3
    return result


def price_profile(
    profile: dict, pool: dict, card: dict, fitted: dict, *, measured: bool = False
) -> dict:
    """Price one joint resource sample; MCP memory persists, FaaS syncs per call.

    Logical accesses describe demand, not storage RPCs. Without a transport
    measurement, per-call snapshots use the peak state size as a conservative
    envelope and report that assumption. No state reload is charged for MCP.
    """
    validate_pool(pool)
    validate_tariff(card, pool["provider"])
    m, resources, rates = profile.get("metrics", {}), pool["resources"], card["rates"]
    missing = []

    def metric(key):
        if not number(m.get(key)):
            missing.append(key)
            return 0.0
        return m[key]

    tool, idle = metric("tool_elapsed_s"), metric("llm_idle_s")
    same_environment = profile.get("execution_pool") == execution_context(pool)
    if measured and not same_environment:
        raise ValueError("Measured pricing requires the actual execution environment")
    native = same_environment and not profile.get("reference_id")
    observed = measured or native
    shared_basis = bool(
        profile.get("reference_id")
        and profile["reference_id"] == fitted.get("reference_id")
    )
    scale = fitted["tool_scale"] if shared_basis and not observed else 1.0
    init = fitted["init_s"] if not observed else 0.0
    call_counts = profile.get("workflow", {}).get("tool_calls", {}).values()
    if any(not number(v) or int(v) != v for v in call_counts):
        raise ValueError("Tool invocation counts must be nonnegative integers")
    calls = sum(call_counts)
    if "tool_calls" not in profile.get("workflow", {}):
        missing.append("tool_calls")
    memory = resources.get("memory_mb", fitted.get("memory_mb"))
    cpu = resources.get("cpu", fitted.get("cpu"))
    if not number(memory):
        missing.append("memory_mb")
        memory = 0
    if not number(cpu):
        if rates["cpu_s"]:
            missing.append("cpu")
        cpu = 0
    decoupled = BACKENDS[pool["provider"]][0] == "state-decoupled"
    state_size = metric("state_max_object_bytes")
    # shortcut: peak object approximates a snapshot; calibrate transport bytes for multi-object state.
    sync_bytes = state_size * calls * 2 if decoupled else 0
    if decoupled and same_environment and number(m.get("state_transfer_bytes")):
        sync_bytes = m["state_transfer_bytes"]
    reads = writes = calls if decoupled else 0
    for field, value in (("state_store_reads", reads), ("state_store_writes", writes)):
        if same_environment and number(m.get(field)):
            if field.endswith("reads"):
                reads = m[field]
            else:
                writes = m[field]
    sync = (
        (sync_bytes * fitted["sync_s_per_byte"] + calls * fitted["sync_s_per_call"])
        if decoupled and not observed
        else 0
    )
    active = (
        (
            m["runtime_tool_s"]
            if observed and number(m.get("runtime_tool_s"))
            else tool * scale
        )
        + init
        + sync
    )
    durations = [
        v for values in profile.get("tool_durations", {}).values() for v in values
    ]
    if observed and profile.get("runtime_tool_durations"):
        durations = profile["runtime_tool_durations"]
    tool_wall, execution_wall = m.get("tool_active_s"), m.get("execution_active_s")
    if not number(tool_wall) or not number(execution_wall):
        if profile.get("workflow", {}).get("parallel_peak") == 1:
            tool_wall, execution_wall = tool, tool + idle
        elif not observed:
            missing.append("overlap_timing")
            tool_wall, execution_wall = tool, tool + idle
    overlap = min(1.0, tool_wall / tool) if tool and number(tool_wall) else 1.0
    active_wall = active * overlap
    wall = (
        metric("agent_elapsed_s")
        if observed
        else active_wall + max(0, execution_wall - tool_wall)
    )
    quantum = card["quantum_s"]
    if not decoupled:
        billed = math.ceil(active_wall / quantum) * quantum
    elif calls and len(durations) == calls and all(number(v) for v in durations):
        billed = sum(
            math.ceil((v * scale + (sync + init) / calls) / quantum) * quantum
            for v in durations
        )
    else:
        # shortcut: missing call durations use an upper rounding bound, replace with per-call profiles.
        billed = active + quantum * calls
    if decoupled and shared_basis and not observed and "billed_scale" in fitted:
        billed = tool * fitted["billed_scale"] + quantum * calls
    cpu_seconds = billed * cpu
    if card["cpu_basis"] == "active":
        cpu_seconds = metric("tool_cpu_s") * scale + (sync + init) * cpu
        rounding_base = active if decoupled else active_wall
        if rounding_base:
            cpu_seconds *= billed / rounding_base
    session_s = wall + card["session_tail_s"]
    if card["memory_basis"] == "session":
        session_s = math.ceil(session_s / quantum) * quantum
    memory_billed = (
        min(billed, session_s) if card["memory_basis"] == "session" else billed
    )
    retention_s = (
        max(0, session_s - memory_billed) if card["memory_basis"] == "session" else 0
    )
    memory_gb = memory / 1024
    execution = (
        cpu_seconds * rates["cpu_s"] + memory_gb * memory_billed * rates["memory_gb_s"]
    )
    retention = memory_gb * retention_s * rates["memory_gb_s"]
    disk_mb = resources.get(
        "disk_mb", resources.get("ephemeral_storage", {}).get("Size")
    )
    if not number(disk_mb):
        if rates["disk_gb_s"]:
            missing.append("disk_mb")
        disk_mb = 0
    disk = max(0, disk_mb / 1024 - card["free_disk_gb"])
    execution += disk * memory_billed * rates["disk_gb_s"]
    retention += disk * retention_s * rates["disk_gb_s"]
    if card["compute_in_pool_floor"]:
        execution = retention = 0.0
    execution += calls * rates["invocation"]
    execution_details = {
        "cpu": 0 if card["compute_in_pool_floor"] else cpu_seconds * rates["cpu_s"],
        "memory": 0
        if card["compute_in_pool_floor"]
        else memory_gb * memory_billed * rates["memory_gb_s"],
        "disk": 0
        if card["compute_in_pool_floor"]
        else disk * memory_billed * rates["disk_gb_s"],
        "request": calls * rates["invocation"],
    }
    storage = reads * rates["read"] + writes * rates["write"]
    storage += (
        state_size / 1e9 * (session_s + card["storage_tail_s"]) * rates["storage_gb_s"]
        if decoupled or reads or writes
        else 0
    )
    storage += sync_bytes / 1e9 * card["transfer_fraction"] * rates["transfer_gb"]
    components = {
        "execution": execution,
        "retention": retention,
        "storage": storage,
        "scaling": 0.0,
    }
    return {
        "components": components,
        "execution_details": execution_details,
        "execution_complete": not set(missing)
        & (
            {
                "tool_elapsed_s",
                "tool_calls",
                "memory_mb",
                "cpu",
                "tool_cpu_s",
                "disk_mb",
                "overlap_timing",
                "agent_elapsed_s",
            }
            | (
                {"state_max_object_bytes"}
                if decoupled
                and fitted["sync_s_per_byte"]
                and "billed_scale" not in fitted
                else set()
            )
        ),
        "total": sum(components.values()),
        "latency_s": wall,
        "session_s": session_s,
        "calibrated": measured
        or (native and number(m.get("runtime_tool_s")))
        or (shared_basis and fitted["calibrated"]),
        "timing_basis": "measured"
        if measured
        else "target_native"
        if native
        else "shared_reference"
        if shared_basis
        else "unverified_reference",
        "usage": {
            "cpu_s": cpu_seconds,
            "billed_s": billed,
            "memory_gb": memory_gb,
            "read": reads,
            "write": writes,
            "transfer_bytes": sync_bytes,
            "invocations": calls,
        },
        "missing": sorted(set(missing)),
    }


def predict_cost(
    profiles: list[dict],
    pool: dict,
    card: dict,
    rows: list[dict],
    *,
    measured: bool = False,
) -> dict:
    """Return empirical demand uncertainty priced by a tariff, never price neighbors."""
    validate_tariff(card, pool["provider"])
    fits = {
        basis: calibration(card, rows, basis)
        for basis in {p.get("reference_id") for p in profiles}
    }
    fitted = next(iter(fits.values()), calibration(card, []))
    samples = [
        price_profile(p, pool, card, fits[p.get("reference_id")], measured=measured)
        for p in profiles
    ]
    complete = bool(samples) and all(not s["missing"] for s in samples)
    return {
        "kind": "request",
        "scope": "marginal_infrastructure",
        "basis": "measured_usage_estimate" if measured else "analytical",
        "currency": card["currency"],
        "tariff": card,
        "calibration": fitted,
        "complete": complete,
        "calibrated": bool(samples) and all(s["calibrated"] for s in samples),
        "total": distribution([s["total"] for s in samples], len(samples)),
        "components": {
            k: distribution([s["components"][k] for s in samples], len(samples))
            for k in ("execution", "retention", "storage", "scaling")
        },
        "execution_complete": bool(samples)
        and all(s["execution_complete"] for s in samples),
        "execution_details": {
            k: distribution([s["execution_details"][k] for s in samples], len(samples))
            for k in ("cpu", "memory", "disk", "request")
        },
        "latency": distribution([s["latency_s"] for s in samples], len(samples)),
        "missing": sorted({k for s in samples for k in s["missing"]}),
        "resource_samples": samples,
        "range_basis": "empirical demand range; not a guaranteed budget bound",
    }


def pool_cost(
    card: dict,
    *,
    requests: float,
    marginal: dict | None,
    horizon_s: float,
    changes=0,
    overlap_s=0.0,
    retained_s=0.0,
    memory_mb=0.0,
) -> dict:
    """Count the entire pool floor/shared service once, plus marginal request usage."""
    if any(
        not number(v)
        for v in (requests, horizon_s, changes, overlap_s, retained_s, memory_mb)
    ):
        raise ValueError("Invalid pool usage")
    if requests and (not marginal or not marginal["complete"]):
        raise ValueError("Missing resource demand for pool forecast")
    rates = card["rates"]
    components = {
        k: requests * marginal["components"][k]["mean"] if requests else 0.0
        for k in ("execution", "retention", "storage", "scaling")
    }
    components["retention"] += horizon_s * rates["pool_per_s"]
    if card["memory_basis"] == "session" and not card["compute_in_pool_floor"]:
        components["retention"] += retained_s * memory_mb / 1024 * rates["memory_gb_s"]
    components["scaling"] += (
        changes * rates["scale_event"] + overlap_s * rates["pool_per_s"]
    )
    components["shared"] = horizon_s * rates["shared_per_s"]
    return {
        "basis": "analytical",
        "scope": "total_infrastructure",
        "currency": card["currency"],
        "components": components,
        "total": sum(components.values()),
        "evidence": card["source"],
    }
