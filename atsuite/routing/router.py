"""Causal empirical risk calibration and whole-session admission."""

import copy
import json
import math
import threading
import time
from pathlib import Path

from atsuite.provider_model.model import _compatible
from atsuite.provider_model.observation import validate_pool
from atsuite.workload.profile import number


def risk_estimate(rows: list[dict], slo_s: float) -> dict:
    """Wilson 95% upper bound over unique attempts, including failures/unknowns.

    This interval assumes exchangeable Bernoulli attempts, not a queueing SLA.
    """
    n = len(rows)
    bad = sum(
        r["status"] != "success"
        or not number(r["measurements"].get("latency_s"))
        or r["measurements"]["latency_s"] > slo_s
        for r in rows
    )
    if not n:
        return {"n": 0, "violations": 0, "probability": None, "upper": 1.0}
    p, z = bad / n, 1.96
    upper = (
        p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    ) / (1 + z * z / n)
    return {"n": n, "violations": bad, "probability": p, "upper": upper}


class OnlineRouter:
    """One owner per pool; limits are measured session admission limits, not RPC slots.

    Callers must include external occupancy by reducing limits before construction.
    Unknown limits reject admission. No migration or automatic retry is performed.
    """

    def __init__(
        self,
        workload,
        provider,
        pools: dict,
        limits: dict,
        *,
        slo_s=120.0,
        max_risk=0.05,
        min_samples=60,
        log_path: Path | None = None,
    ):
        if (
            not number(slo_s)
            or slo_s == 0
            or not number(max_risk)
            or not 0 < max_risk < 1
        ):
            raise ValueError("Invalid SLO/risk constraint")
        if (
            isinstance(min_samples, bool)
            or not isinstance(min_samples, int)
            or min_samples < 1
        ):
            raise ValueError("Invalid minimum sample count")
        for pool in pools.values():
            validate_pool(pool)
        if len({json.dumps(p, sort_keys=True) for p in pools.values()}) != len(pools):
            raise ValueError("Duplicate execution pools")
        for key, limit in limits.items():
            if key not in pools or (
                limit is not None
                and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0)
            ):
                raise ValueError("Invalid session admission limit")
        self.workload, self.provider = workload, provider
        self.pools, self.limits = copy.deepcopy(pools), dict(limits)
        self.slo_s, self.max_risk, self.min_samples = slo_s, max_risk, min_samples
        self.log_path = Path(log_path) if log_path else None
        # shortcut: retain decisions in memory; bound retention for longer-running studies.
        self.active, self.decisions, self.completed = {}, {}, set()
        self.lock = threading.RLock()

    def _log(self, event):
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as stream:
                stream.write(json.dumps(event, allow_nan=False) + "\n")

    def set_capacity(self, key: str, pool: dict, limit: int | None) -> None:
        """Publish read-back capacity atomically without rebinding active sessions."""
        validate_pool(pool)
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("Invalid session admission limit")
        with self.lock:
            old = self.pools[key]
            if any(old[k] != pool[k] for k in ("provider", "environment")):
                raise ValueError("Capacity updates cannot replace an execution pool")
            if old != pool:
                for session in self.active.values():
                    if session["selected"] == key:
                        session["capacity_transition"] = True
            self.pools[key], self.limits[key] = copy.deepcopy(pool), limit

    def decide(
        self,
        request_id: str,
        task: str,
        load_rps: float,
        *,
        as_of_ns=None,
        shadow=False,
        pool_load_rps: dict | None = None,
        allowed_pools: set | None = None,
        arrival_ns: int | None = None,
    ) -> dict:
        """Freeze pre-dispatch predictions and reserve a Session within its remaining SLO."""
        now = time.time_ns() if as_of_ns is None else as_of_ns
        arrival = now if arrival_ns is None else arrival_ns
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(task, str)
            or not task
            or not isinstance(shadow, bool)
            or not number(load_rps)
            or not number(now)
            or not number(arrival)
            or arrival > now
        ):
            raise ValueError("Invalid routing input")
        rates = (
            dict(pool_load_rps)
            if pool_load_rps is not None
            else dict.fromkeys(self.pools, load_rps)
        )
        if set(rates) != set(self.pools) or any(not number(v) for v in rates.values()):
            raise ValueError("Every pool needs a finite nonnegative load")
        if allowed_pools is not None and not set(allowed_pools) <= self.pools.keys():
            raise ValueError("Unknown routing policy pool")
        with self.lock:
            if request_id in self.decisions:
                return copy.deepcopy(self.decisions[request_id])
            with self.provider.lock:
                if any(key[3] == request_id for key in self.provider.samples):
                    raise ValueError("Request ID already exists in provider history")
            workload = self.workload.predict(now)
            profiles = workload["joint_profiles"][-20:]
            predictions = [
                self.provider.predict(profiles, [pool], rates[key], as_of_ns=now)[
                    "pools"
                ][0]
                for key, pool in self.pools.items()
            ]
            with self.provider.lock:
                visible = [
                    eligible[-1]
                    for history in self.provider.samples.values()
                    if (eligible := [r for r in history if r["available_ns"] < now])
                ]
            candidates = []
            for (key, pool), pred in zip(self.pools.items(), predictions):
                rate = rates[key]
                # shortcut: exact task and load envelope; broaden only after independent validation.
                rows = [
                    r
                    for r in visible
                    if r["kind"] == "request"
                    and not r.get("capacity_transition")
                    and _compatible(r["pool"], pool)
                    and r["pool"].get("instances") == pool.get("instances")
                    and r.get("task_id") == task
                    and r["load_rps"] >= rate
                    and r["load_rps"] <= max(rate * 1.25, rate + 0.01)
                ]
                remaining_slo_s = max(0, self.slo_s - (now - arrival) / 1e9)
                risk = risk_estimate(rows, remaining_slo_s)
                limit = self.limits.get(key)
                occupied = sum(v["selected"] == key for v in self.active.values())
                flags = []
                if remaining_slo_s == 0:
                    flags.append("slo_budget_exhausted")
                if allowed_pools is not None and key not in allowed_pools:
                    flags.append("excluded_by_control_policy")
                if limit is None:
                    flags.append("capacity_unknown")
                elif occupied >= limit:
                    flags.append("capacity_exhausted")
                if risk["n"] < self.min_samples:
                    flags.append("insufficient_task_load_evidence")
                if risk["upper"] > self.max_risk:
                    flags.append("risk_upper_exceeds_limit")
                costs = [
                    c
                    for c in pred["cost"]
                    if c["kind"] == "request"
                    and c["complete"]
                    and c["total"]["mean"] is not None
                ]
                candidates.append(
                    {
                        "key": key,
                        "prediction": pred,
                        "risk": risk,
                        "flags": flags,
                        "available_sessions": None
                        if limit is None
                        else max(0, limit - occupied),
                        "cost": costs[0] if len(costs) == 1 else None,
                    }
                )
            feasible = [c for c in candidates if not c["flags"]]
            reliable = [c for c in feasible if not c["prediction"]["low_confidence"]]
            choices = reliable or feasible
            comparable = (
                bool(choices)
                and all(c["cost"] for c in choices)
                and len(
                    {
                        (c["cost"]["currency"], c["cost"]["scope"])
                        for c in choices
                        if c["cost"]
                    }
                )
                == 1
            )
            selected = (
                min(
                    choices,
                    key=lambda c: (
                        c["cost"]["total"]["mean"]
                        if comparable
                        else c["risk"]["upper"],
                        c["key"],
                    ),
                )
                if choices
                else None
            )
            result = {
                "request_id": request_id,
                "task_id": task,
                "arrival_ns": arrival,
                "decision_ns": now,
                "load_rps": load_rps,
                "pool_load_rps": rates,
                "load_basis": "per_pool_snapshot"
                if pool_load_rps is not None
                else "offered_load_per_candidate",
                "slo_s": self.slo_s,
                "remaining_slo_s": max(0, self.slo_s - (now - arrival) / 1e9),
                "max_risk": self.max_risk,
                "shadow": shadow,
                "selected": selected["key"] if selected else None,
                "pool_snapshot": copy.deepcopy(self.pools),
                "mode": selected["prediction"]["mode"] if selected else None,
                "reason": (
                    "cost_within_risk_constraint" if comparable else "lowest_risk"
                )
                if reliable
                else "empirical_fallback"
                if choices
                else "no_trusted_feasible_pool",
                "cost_comparable": comparable,
                "ranking": "predicted_scoped_cost" if comparable else "risk_upper",
                "slo_guarantee": False,
                "workload": workload,
                "provider_workload_profiles": len(profiles),
                "candidates": candidates,
            }
            self._log({"event": "decision", **result})
            self.decisions[request_id] = copy.deepcopy(result)
            if selected and not shadow:
                self.active[request_id] = copy.deepcopy(result)
            return result

    def complete(
        self, request_id: str, trace: dict, *, available_ns=None, actual_pool=None
    ) -> dict:
        """Feedback goes only to the bound pool; failed traces also release admission."""
        from atsuite.workload.profile import extract_profile

        now = time.time_ns() if available_ns is None else available_ns
        with self.lock:
            decision = self.decisions[request_id]
            if request_id in self.completed:
                raise ValueError("Request already completed")
            if decision["shadow"]:
                if actual_pool not in self.pools:
                    raise ValueError(
                        "Shadow completion requires the actual execution pool"
                    )
                selected = actual_pool
            else:
                selected = self.active[request_id]["selected"]
                if actual_pool is not None and actual_pool != selected:
                    raise ValueError("Session migration is forbidden")
            profile = extract_profile(trace)
            if (
                profile["request_id"] != request_id
                or trace["arrival_ns"] != decision["arrival_ns"]
            ):
                raise ValueError("Completion identity/arrival differs from admission")
            row = {
                "kind": "request",
                "id": request_id,
                "task_id": decision["task_id"],
                "profile": profile,
                "pool": copy.deepcopy(decision["pool_snapshot"][selected]),
                "capacity_transition": self.active.get(request_id, {}).get(
                    "capacity_transition", False
                ),
                "load_rps": decision["pool_load_rps"][selected],
                "status": profile["status"],
                "arrival_ns": trace["arrival_ns"],
                "completed_ns": trace["completed_ns"],
                "available_ns": now,
                "cost": None,
                "measurements": {
                    "latency_s": (trace["completed_ns"] - trace["arrival_ns"]) / 1e9
                },
            }
            self.provider.update(row)
            self.workload.update(profile, now)
            self._log(
                {
                    "event": "completion",
                    "request_id": request_id,
                    "selected": selected,
                    "observation": row,
                }
            )
            self.active.pop(request_id, None)
            self.completed.add(request_id)
            return row
