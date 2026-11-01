"""Exclusive infrastructure cost components and a frozen currency reference."""

from decimal import Decimal

from atsuite.routing.capacity import count
from atsuite.workload.profile import number

COMPONENTS = {"execution", "retention", "storage", "scaling"}


def forecast_cost(
    cost: dict,
    *,
    requests: dict,
    horizon_s: float,
    retained_s: float,
    changes: int,
    fx: dict,
) -> dict:
    """Forecast a pool once; execution excludes all shared/idle/storage charges.

    retention_per_s is the entire configured pool floor (busy and idle included).
    session_per_s covers ONLY additional managed-session residency outside that
    floor. Explicit zeros and evidence are required, even for unused components.
    """
    if (
        cost.get("complete") is not True
        or not cost.get("evidence")
        or cost.get("scope") != "total_infrastructure"
        or cost.get("exclusive_components") != sorted(COMPONENTS)
    ):
        raise ValueError("Complete, disjoint infrastructure cost evidence required")
    rates = [
        cost[k]
        for k in ("retention_per_s", "session_per_s", "storage_per_s", "per_change")
    ]
    rates += list(cost["per_request"].values())
    if any(
        not number(x)
        for x in rates + [horizon_s, retained_s, changes] + list(requests.values())
    ):
        raise ValueError("Invalid cost rate or usage")
    if (
        not fx.get("date")
        or not fx.get("source")
        or not fx.get("currency")
        or fx.get("rates", {}).get(fx["currency"]) != 1
    ):
        raise ValueError("A dated currency conversion reference is required")
    rate = fx["rates"].get(cost["currency"])
    if not number(rate) or rate == 0:
        raise ValueError("Missing positive currency conversion rate")
    d = lambda value: Decimal(str(value))
    components = {
        "execution": sum(
            (
                d(count) * d(cost["per_request"][task])
                for task, count in requests.items()
            ),
            Decimal(0),
        ),
        "retention": d(horizon_s) * d(cost["retention_per_s"])
        + d(retained_s) * d(cost["session_per_s"]),
        "storage": d(horizon_s) * d(cost["storage_per_s"]),
        "scaling": d(changes) * d(cost["per_change"]),
    }
    return {
        "basis": "predicted",
        "scope": "total_infrastructure",
        "currency": fx["currency"],
        "fx": fx,
        "evidence": cost["evidence"],
        "components": {k: float(v * d(rate)) for k, v in components.items()},
        "total": float(sum(components.values()) * d(rate)),
    }


def total_cost(lines: list[dict], fx: dict, *, components=COMPONENTS) -> dict:
    """Deduplicate attributable charge IDs; inclusive windows replace their children.

    Input scopes are resource + billing interval + component. Mixing invoice and
    estimate for that scope is rejected rather than silently adding both.
    """
    seen, scopes, totals, bases = {}, set(), {}, set()
    if (
        not fx.get("date")
        or not fx.get("source")
        or not fx.get("currency")
        or fx.get("rates", {}).get(fx["currency"]) != 1
    ):
        raise ValueError("Invalid currency reference")
    for row in lines:
        count(row["start_ns"])
        count(row["end_ns"])
        identity = (row["provider"], row["charge_id"])
        if identity in seen:
            if row != seen[identity]:
                raise ValueError("Conflicting charge revision")
            continue
        scope = (
            row["provider"],
            row["resource"],
            row["start_ns"],
            row["end_ns"],
            row["component"],
        )
        if scope in scopes or row["component"] not in components:
            raise ValueError("Overlapping or unknown charge component")
        for previous in seen.values():
            if (
                previous["provider"] == row["provider"]
                and previous["resource"] == row["resource"]
                and previous["component"] == row["component"]
                and max(previous["start_ns"], row["start_ns"])
                < min(previous["end_ns"], row["end_ns"])
            ):
                raise ValueError("Overlapping charge intervals")
        if (
            not number(row["amount"])
            or not row.get("evidence")
            or row.get("basis") not in {"predicted", "estimated", "actual"}
            or row["start_ns"] >= row["end_ns"]
            or row.get("attribution") != "exclusive"
        ):
            raise ValueError("Invalid charge evidence")
        rate = fx["rates"].get(row["currency"])
        if not fx.get("date") or not fx.get("source") or not number(rate) or rate == 0:
            raise ValueError("Missing currency reference")
        totals[row["component"]] = totals.get(row["component"], Decimal(0)) + Decimal(
            str(row["amount"])
        ) * Decimal(str(rate))
        seen[identity] = row
        scopes.add(scope)
        bases.add(row["basis"])
    resource_components = {}
    for row in seen.values():
        resource_components.setdefault(
            (row["provider"], row["resource"]), {}
        ).setdefault(row["component"], []).append((row["start_ns"], row["end_ns"]))

    def coverage(intervals):
        merged = []
        for start, end in sorted(intervals):
            if merged and merged[-1][1] == start:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        return tuple(merged)

    complete = (
        bool(seen)
        and all(
            set(c) == set(components) and len({coverage(v) for v in c.values()}) == 1
            for c in resource_components.values()
        )
        and all(r.get("complete") is True for r in seen.values())
    )
    return {
        "scope": "supplied_resources_only",
        "resources": sorted(resource_components),
        "basis": next(iter(bases))
        if len(bases) == 1
        else "mixed"
        if bases
        else "unknown",
        "currency": fx["currency"],
        "fx": fx,
        "complete": complete,
        "total": float(sum(totals.values())) if complete else None,
        "subtotal": float(sum(totals.values())),
        "components": {k: float(v) for k, v in totals.items()},
        "charge_count": len(seen),
    }
