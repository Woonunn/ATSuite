"""Resource-demand pricing with explicit, dated tariffs and disjoint pool charges."""

import math
from statistics import median

from atsuite.provider_model.observation import BACKENDS, validate_pool
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
    if any(not number(v) for k, v in calibration.items() if k != "evidence"):
        raise ValueError("Invalid platform calibration")
    if calibration and not calibration.get("evidence"):
        raise ValueError("Platform calibration needs evidence")


def calibration(card: dict, rows: list[dict]) -> dict:
    """Fit overheads from visible profiling rows, without fitting historical prices."""
    result = {
        "tool_scale": 1.0,
        "init_s": 0.0,
        "sync_s_per_byte": 0.0,
        "sync_s_per_call": 0.0,
        **card.get("calibration", {}),
    }
    ratios, inits, syncs, billed_ratios = [], [], [], []
    for row in rows:
        m, p = row["measurements"], row["profile"].get("metrics", {})
        if (
            number(m.get("tool_s"))
            and number(p.get("tool_elapsed_s"))
            and p["tool_elapsed_s"] > 0
        ):
            # Runtime execution_elapsed_ms includes restore/persist; price synchronization once.
            sync = m.get("state_sync_s") if number(m.get("state_sync_s")) else 0
            ratios.append(max(0, m["tool_s"] - sync) / p["tool_elapsed_s"])
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
    result["sample_count"] = len(rows)
    return result


def price_profile(profile: dict, pool: dict, card: dict, fitted: dict) -> dict:
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
    if decoupled and number(m.get("state_transfer_bytes")):
        sync_bytes = m["state_transfer_bytes"]
    reads = writes = calls if decoupled else 0
    for field, value in (("state_store_reads", reads), ("state_store_writes", writes)):
        if number(m.get(field)):
            if field.endswith("reads"):
                reads = m[field]
            else:
                writes = m[field]
    sync = (
        (sync_bytes * fitted["sync_s_per_byte"] + calls * fitted["sync_s_per_call"])
        if decoupled
        else 0
    )
    active = tool * fitted["tool_scale"] + fitted["init_s"] + sync
    durations = [
        v for values in profile.get("tool_durations", {}).values() for v in values
    ]
    quantum = card["quantum_s"]
    if not decoupled:
        billed = math.ceil(active / quantum) * quantum
    elif calls and len(durations) == calls and all(number(v) for v in durations):
        billed = sum(
            math.ceil(
                (v * fitted["tool_scale"] + (sync + fitted["init_s"]) / calls) / quantum
            )
            * quantum
            for v in durations
        )
    else:
        # shortcut: missing call durations use an upper rounding bound, replace with per-call profiles.
        billed = active + quantum * calls
    if decoupled and "billed_scale" in fitted:
        billed = tool * fitted["billed_scale"] + quantum * calls
    cpu_seconds = billed * cpu
    if card["cpu_basis"] == "active":
        cpu_seconds = (
            metric("tool_cpu_s") * fitted["tool_scale"]
            + (sync + fitted["init_s"]) * cpu
        )
        if active:
            cpu_seconds *= billed / active
    session_s = active + idle + card["session_tail_s"]
    if card["memory_basis"] == "session":
        session_s = math.ceil(session_s / quantum) * quantum
    retention_s = max(0, session_s - billed) if card["memory_basis"] == "session" else 0
    memory_gb = memory / 1024
    execution = cpu_seconds * rates["cpu_s"] + memory_gb * billed * rates["memory_gb_s"]
    retention = memory_gb * retention_s * rates["memory_gb_s"]
    disk_mb = resources.get(
        "disk_mb", resources.get("ephemeral_storage", {}).get("Size")
    )
    if not number(disk_mb):
        if rates["disk_gb_s"]:
            missing.append("disk_mb")
        disk_mb = 0
    disk = max(0, disk_mb / 1024 - card["free_disk_gb"])
    execution += disk * billed * rates["disk_gb_s"]
    retention += disk * retention_s * rates["disk_gb_s"]
    if card["compute_in_pool_floor"]:
        execution = retention = 0.0
    execution += calls * rates["invocation"]
    execution_details = {
        "cpu": 0 if card["compute_in_pool_floor"] else cpu_seconds * rates["cpu_s"],
        "memory": 0
        if card["compute_in_pool_floor"]
        else memory_gb * billed * rates["memory_gb_s"],
        "disk": 0
        if card["compute_in_pool_floor"]
        else disk * billed * rates["disk_gb_s"],
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
        "latency_s": active + idle,
        "session_s": session_s,
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
    profiles: list[dict], pool: dict, card: dict, rows: list[dict]
) -> dict:
    """Return empirical demand uncertainty priced by a tariff, never price neighbors."""
    validate_tariff(card, pool["provider"])
    fitted = calibration(card, rows)
    samples = [price_profile(p, pool, card, fitted) for p in profiles]
    complete = bool(samples) and all(not s["missing"] for s in samples)
    return {
        "kind": "request",
        "scope": "marginal_infrastructure",
        "basis": "analytical",
        "currency": card["currency"],
        "tariff": card,
        "calibration": fitted,
        "complete": complete,
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
