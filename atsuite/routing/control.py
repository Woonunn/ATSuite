"""Finite joint routing/capacity search with causal risk and drain protection."""

import copy
import itertools
import math
import time
from collections import Counter, deque

from atsuite.provider_model.observation import validate_pool
from atsuite.routing.capacity import count, session_retention_s
from atsuite.routing.cost import forecast_cost
from atsuite.routing.router import OnlineRouter
from atsuite.workload.profile import number


def candidate_plans(levels: dict, weights: list[dict], *, maximum=256) -> list[dict]:
    """Enumerate an explicit, bounded grid; never silently truncate the search."""
    size = math.prod(len(v) for v in levels.values()) * len(weights)
    if not 0 < size <= maximum:
        raise ValueError("Candidate grid is empty or exceeds the search bound")
    keys = list(levels)
    return [
        {"targets": dict(zip(keys, targets)), "weights": dict(weight)}
        for targets in itertools.product(*levels.values())
        for weight in weights
    ]


class JointController:
    """Single-owner controller, usable by the existing LangGraph routing hook.

    Levels map each pool and target to a measured pool configuration, Session
    limit, scaling-delay upper bound and complete disjoint infrastructure rates.
    A None target denotes a platform-managed pool. Plans specify traffic shares;
    current requests use only a successfully read-back, risk-checked level.
    """

    def __init__(
        self,
        router: OnlineRouter,
        adapters: dict,
        levels: dict,
        plans: list[dict],
        fx: dict,
        *,
        period_s=10.0,
        horizon_s=60.0,
        cooldown_s=60.0,
        observation_s=30.0,
        target_utilization=0.7,
        policy="joint",
    ):
        keys = set(router.pools)
        if (
            keys != set(adapters)
            or keys != set(levels)
            or not plans
            or len(plans) > 256
        ):
            raise ValueError(
                "Every execution pool needs an adapter and bounded candidates"
            )
        if policy not in {"joint", "independent", "routing", "static"}:
            raise ValueError("Unknown control policy")
        if any(
            not number(x) or x == 0 for x in (period_s, horizon_s, observation_s)
        ) or not number(cooldown_s):
            raise ValueError("Invalid control interval")
        if not number(target_utilization) or not 0 < target_utilization <= 1:
            raise ValueError("Invalid utilization target")
        self.router, self.adapters = router, adapters
        self.levels, self.plans, self.fx = (
            copy.deepcopy(levels),
            copy.deepcopy(plans),
            copy.deepcopy(fx),
        )
        for key, options in self.levels.items():
            if not options:
                raise ValueError("Empty capacity levels")
            for target, level in options.items():
                count(target, optional=True)
                count(level["session_limit"])
                validate_pool(level["pool"])
                session_retention_s(level["pool"])
                if level["pool"].get("instances") is not None:
                    raise ValueError(
                        "Serverless controls do not establish a fixed physical instance count"
                    )
                if (
                    not level.get("evidence")
                    or not number(level["scale_delay_s"])
                    or level["pool"]["provider"] != router.pools[key]["provider"]
                    or level["pool"]["environment"] != router.pools[key]["environment"]
                ):
                    raise ValueError(
                        "Capacity level needs matching deployment and calibration evidence"
                    )
                expected = {"control": adapters[key].control, "target": target}
                if level["pool"]["resources"].get("capacity_control") != expected:
                    raise ValueError(
                        "Pool identity must include the capacity control and target"
                    )
                if (target is None) != (adapters[key].control == "managed"):
                    raise ValueError(
                        "Managed capacity cannot have an invented instance target"
                    )
            if len({str(v["pool"]) for v in options.values()}) != len(options):
                raise ValueError(
                    "Capacity levels must have distinct calibration identities"
                )
        for plan in self.plans:
            if set(plan["targets"]) != keys or set(plan["weights"]) != keys:
                raise ValueError("Incomplete joint plan")
            if any(not number(w) for w in plan["weights"].values()) or not math.isclose(
                sum(plan["weights"].values()), 1
            ):
                raise ValueError("Routing weights must sum to one")
            if any(
                target not in self.levels[key]
                for key, target in plan["targets"].items()
            ):
                raise ValueError("Candidate exceeds calibrated capacity bounds")
        self.period_s, self.horizon_s, self.cooldown_s = period_s, horizon_s, cooldown_s
        self.observation_s, self.target_utilization, self.policy = (
            observation_s,
            target_utilization,
            policy,
        )
        self.snapshots, self.pending, self.draining, self.last_change = {}, {}, {}, {}
        self.arrivals, self.routed = deque(), deque()
        # shortcut: deduplicate for one experiment; bound retention before operating as a long-lived service.
        self.offered_ids = set()
        self.last_tick, self.plan, self.initial = None, None, None
        self.deficit = Counter()
        self.release_holds = set()
        self.retained_until = {}
        self.last_report = None

    @property
    def pools(self):
        return self.router.pools

    @property
    def active(self):
        return self.router.active

    @property
    def decisions(self):
        return self.router.decisions

    def _log(self, event, **data):
        self.router._log({"event": event, **data})

    def _occupied(self, key):
        return sum(d["selected"] == key for d in self.active.values())

    def hold_release(self, request_id: str) -> None:
        """Retain occupancy when remote Session teardown was not confirmed."""
        with self.router.lock:
            self.release_holds.add(request_id)
            self._log("session_release_unconfirmed", request_id=request_id)

    def complete(self, request_id: str, trace: dict, **kwargs) -> dict:
        """Feed back exactly once; uncertain remote cleanup keeps its reservation."""
        with self.router.lock:
            bound = copy.deepcopy(self.active.get(request_id))
            result = self.router.complete(request_id, trace, **kwargs)
            if request_id in self.release_holds and bound:
                self.active[request_id] = bound
            elif bound:
                retention = session_retention_s(result["pool"])
                if retention:
                    self.active[request_id] = bound
                    self.retained_until[request_id] = result["available_ns"] + int(
                        retention * 1e9
                    )
                    self._log(
                        "session_retained",
                        request_id=request_id,
                        until_ns=self.retained_until[request_id],
                        basis="configured_maximum_lifetime_after_http_close",
                    )
            return result

    def expire_retained(self, now_ns: int) -> None:
        """Release local holds only after the conservative configured lifetime bound."""
        with self.router.lock:
            for rid, deadline in list(self.retained_until.items()):
                if now_ns >= deadline:
                    self.retained_until.pop(rid)
                    self.active.pop(rid, None)
                    self._log(
                        "session_retention_elapsed",
                        request_id=rid,
                        observed_ns=now_ns,
                        physical_release_observed=False,
                    )

    def _refresh(self, now):
        self.expire_retained(now)
        for key, adapter in self.adapters.items():
            try:
                state = adapter.read(now)
                for field, value in getattr(adapter, "configuration", {}).items():
                    expected = self.pools[key]["resources"].get(field)
                    if field in self.pools[key]["resources"] and expected != value:
                        raise ValueError(
                            "Live deployment configuration drift: " + field
                        )
                self.snapshots[key] = state
                self._log(
                    "capacity_observation",
                    pool=key,
                    **state.to_dict(),
                    active_sessions=sum(
                        d["selected"] == key and rid not in self.router.completed
                        for rid, d in self.active.items()
                    ),
                    occupied_sessions=self._occupied(key),
                    session_capacity=self.levels[key]
                    .get(state.effective, {})
                    .get("session_limit")
                    if state.status == "READY"
                    else None,
                    retained_sessions=sum(
                        self.active[rid]["selected"] == key
                        for rid in self.retained_until
                    ),
                )
                target = state.effective
                known = state.status == "READY" and target in self.levels[key]
                if adapter.control != "managed" and target is None:
                    known = False
                if known:
                    level = self.levels[key][target]
                    limit = 0 if key in self.draining else level["session_limit"]
                    self.router.set_capacity(key, level["pool"], limit)
                    if key in self.pending and self.pending[key]["target"] == target:
                        request = self.pending.pop(key)
                        self.last_change[key] = now
                        self._log(
                            "scaling_effective",
                            pool=key,
                            target=target,
                            observed_ns=now,
                            requested_ns=request["requested_ns"],
                            observed_delay_s=(now - request["requested_ns"]) / 1e9,
                        )
                else:
                    self.router.set_capacity(key, self.pools[key], None)
            except Exception as exc:  # noqa: BLE001 - any failed SDK read closes admission.
                self.snapshots.pop(key, None)
                self.router.set_capacity(key, self.pools[key], None)
                for session in self.active.values():
                    if session["selected"] == key:
                        session["capacity_transition"] = True
                self._log(
                    "capacity_error",
                    pool=key,
                    observed_ns=now,
                    error=type(exc).__name__,
                )

    def _submit(self, key, target, now):
        state = self.snapshots[key]
        if target == state.target or self.adapters[key].control == "managed":
            return
        if (
            key in self.pending
            or now - self.last_change.get(key, -(10**30)) < self.cooldown_s * 1e9
        ):
            return
        if state.target is None:
            return
        if target < state.target and self._occupied(key):
            self.draining[key] = target
            self.router.set_capacity(key, self.pools[key], 0)
            self._log(
                "scaling_drain",
                pool=key,
                target=target,
                observed_ns=now,
                active_sessions=self._occupied(key),
            )
            return
        self._log("scaling_requested", pool=key, target=target, requested_ns=now)
        # On a timeout the mutation may have succeeded: reconcile, never blind retry.
        self.pending[key] = {
            "target": target,
            "from_target": state.target,
            "requested_ns": now,
        }
        self.last_change[key] = now
        for session in self.active.values():
            if session["selected"] == key:
                session["capacity_transition"] = True
        self.router.set_capacity(key, self.pools[key], None)
        try:
            self.adapters[key].apply(target, active_sessions=self._occupied(key))
        except Exception as exc:  # noqa: BLE001 - ambiguous SDK writes require reconciliation.
            self._log(
                "scaling_error",
                pool=key,
                target=target,
                observed_ns=now,
                error=type(exc).__name__,
                reconciliation_required=True,
            )
        self.draining.pop(key, None)

    def _evaluate(self, plan, task_rates, now, queued):
        pools = {k: self.levels[k][v]["pool"] for k, v in plan["targets"].items()}
        limits = {
            k: self.levels[k][v]["session_limit"] for k, v in plan["targets"].items()
        }
        probe = OnlineRouter(
            self.router.workload,
            self.router.provider,
            pools,
            limits,
            slo_s=self.router.slo_s,
            max_risk=self.router.max_risk,
            min_samples=self.router.min_samples,
        )
        offered = sum(task_rates.values())
        rates = {k: offered * w for k, w in plan["weights"].items()}
        flags, risks, duration = [], {}, {}
        for task, rate in task_rates.items():
            if rate == 0:
                continue
            prediction = probe.decide(
                "control-probe:" + task,
                task,
                offered,
                as_of_ns=now,
                pool_load_rps=rates,
                shadow=True,
            )
            for item in prediction["candidates"]:
                key = item["key"]
                if not plan["weights"][key]:
                    continue
                flags.extend(f"{key}:{task}:{flag}" for flag in item["flags"])
                risks[f"{key}:{task}"] = item["risk"]
                # Whole-Session occupancy includes LLM waits, Tool/state work and queues.
                seconds = item["prediction"]["latency"]["p95"]
                if seconds is None:
                    flags.append(f"{key}:{task}:unknown_session_duration")
                else:
                    duration[key] = max(duration.get(key, 0), seconds)
        costs, demand = {}, {}
        for key, target in plan["targets"].items():
            level, state = self.levels[key][target], self.snapshots.get(key)
            if state is None or state.target not in self.levels[key]:
                flags.append(f"{key}:unknown_current_capacity_cost")
            if key in self.pending and self.pending[key]["target"] != target:
                flags.append(f"{key}:scaling_in_progress")
            occupied = self._occupied(key)
            demand[key] = math.ceil(
                rates[key]
                * (duration.get(key, 0) + session_retention_s(level["pool"]))
                / self.target_utilization
                + queued * plan["weights"][key]
            )
            if demand[key] > limits[key]:
                flags.append(f"{key}:forecast_capacity_exceeded")
            changed = state is None or target != state.target
            if changed and self.adapters[key].control != "managed":
                if level["scale_delay_s"] >= self.horizon_s:
                    flags.append(f"{key}:scaling_outside_horizon")
                if now - self.last_change.get(key, -(10**30)) < self.cooldown_s * 1e9:
                    flags.append(f"{key}:cooldown")
            try:
                # Hold existing Sessions for the whole horizon, including idle tails.
                costs[key] = forecast_cost(
                    level["cost"],
                    requests={
                        t: r * plan["weights"][key] * self.horizon_s
                        for t, r in task_rates.items()
                    },
                    horizon_s=self.horizon_s,
                    retained_s=occupied * self.horizon_s
                    + rates[key]
                    * self.horizon_s
                    * (duration.get(key, 0) + session_retention_s(level["pool"])),
                    changes=int(changed),
                    fx=self.fx,
                )
                if changed and state is not None and state.target in self.levels[key]:
                    old = self.levels[key][state.target]
                    # Unknown drain duration retains the old floor for the whole horizon.
                    overlap_s = level["scale_delay_s"]
                    if target < state.target and occupied:
                        overlap_s = self.horizon_s
                    overlap = forecast_cost(
                        old["cost"],
                        requests={},
                        horizon_s=overlap_s,
                        retained_s=0,
                        changes=0,
                        fx=self.fx,
                    )
                    costs[key]["transition_overlap"] = overlap["components"][
                        "retention"
                    ]
                    costs[key]["total"] += overlap["components"]["retention"]
                if key in self.pending:
                    old = self.levels[key][self.pending[key]["from_target"]]
                    retained = forecast_cost(
                        old["cost"],
                        requests={},
                        horizon_s=self.horizon_s,
                        retained_s=0,
                        changes=0,
                        fx=self.fx,
                    )
                    costs[key]["pending_retention"] = retained["components"][
                        "retention"
                    ]
                    costs[key]["total"] += retained["components"]["retention"]
            except (ValueError, KeyError):
                flags.append(f"{key}:incomplete_cost")
        return {
            "plan": plan,
            "flags": sorted(set(flags)),
            "risk": risks,
            "demand": demand,
            "costs": costs,
            "predicted_total": sum(c["total"] for c in costs.values())
            if len(costs) == len(pools)
            else None,
        }

    def tick(
        self, task_rates: dict, *, now_ns=None, queued=0, utilization=None, apply=True
    ) -> dict:
        """Poll capacity, search candidates, submit changes; never count targets as ready."""
        now = time.time_ns() if now_ns is None else now_ns
        count(now)
        count(queued)
        if not task_rates or any(
            not isinstance(t, str) or not t or not number(r)
            for t, r in task_rates.items()
        ):
            raise ValueError("A causal task arrival-rate snapshot is required")
        utilization = utilization or {}
        if not set(utilization) <= self.pools.keys() or any(
            not number(v) or v > 1 for v in utilization.values()
        ):
            raise ValueError("Invalid utilization snapshot")
        with self.router.lock:
            if self.last_tick is not None and now < self.last_tick:
                raise ValueError("Control clock moved backwards")
            self._refresh(now)
            if len(self.snapshots) != len(self.pools):
                self.plan = None
                return {"reason": "capacity_readback_failed", "selected": None}
            if self.initial is None:
                self.initial = {k: s.target for k, s in self.snapshots.items()}
            utilization = {
                **{
                    key: min(1.0, self._occupied(key) / self.router.limits[key])
                    for key in self.pools
                    if self.router.limits.get(key)
                },
                **utilization,
            }
            if (
                self.last_tick is not None
                and now - self.last_tick < self.period_s * 1e9
            ):
                for key, target in list(self.draining.items()):
                    if apply and not self._occupied(key):
                        self._submit(key, target, now)
                return copy.deepcopy(self.last_report)
            self.last_tick = now
            candidates = [
                self._evaluate(p, task_rates, now, queued) for p in self.plans
            ]
            if self.policy == "joint":
                feasible = [c for c in candidates if not c["flags"]]
                selected = (
                    min(feasible, key=lambda c: c["predicted_total"])
                    if feasible
                    else None
                )
            else:
                # Baselines keep the first declared routing split; routing-only freezes capacity.
                feasible = [
                    c
                    for c in candidates
                    if not [f for f in c["flags"] if not f.endswith(":incomplete_cost")]
                ]
                if self.policy in {"static", "routing"}:
                    feasible = [
                        c for c in feasible if c["plan"]["targets"] == self.initial
                    ]
                if self.policy in {"static", "independent"}:
                    feasible = [
                        c
                        for c in feasible
                        if c["plan"]["weights"] == self.plans[0]["weights"]
                    ]
                if self.policy == "independent":
                    for c in feasible:
                        for key, value in utilization.items():
                            state = self.snapshots[key]
                            if value > self.target_utilization and self.levels[key][
                                c["plan"]["targets"][key]
                            ]["session_limit"] <= self.levels[key].get(
                                state.target, {}
                            ).get("session_limit", 0):
                                c["flags"].append(f"{key}:high_utilization")
                    feasible = [
                        c
                        for c in feasible
                        if not any(f.endswith(":high_utilization") for f in c["flags"])
                    ]
                if self.policy == "routing":
                    comparable = all(c["predicted_total"] is not None for c in feasible)
                    selected = (
                        min(
                            feasible,
                            key=lambda c: (
                                c["predicted_total"]
                                if comparable
                                else max(
                                    (r["upper"] for r in c["risk"].values()), default=1
                                )
                            ),
                        )
                        if feasible
                        else None
                    )
                else:
                    selected = (
                        min(
                            feasible,
                            key=lambda c: sum(
                                self.levels[k][t]["session_limit"]
                                for k, t in c["plan"]["targets"].items()
                            ),
                        )
                        if feasible
                        else None
                    )
            self.plan = copy.deepcopy(selected["plan"]) if selected else None
            for key, target in list(self.draining.items()):
                if self.plan is None or self.plan["targets"][key] != target:
                    self.draining.pop(key)
                    state = self.snapshots[key]
                    if state.status == "READY" and state.effective in self.levels[key]:
                        level = self.levels[key][state.effective]
                        self.router.set_capacity(
                            key, level["pool"], level["session_limit"]
                        )
                    self._log(
                        "scaling_drain_cancelled",
                        pool=key,
                        target=target,
                        observed_ns=now,
                    )
            self.last_report = {
                "observed_ns": now,
                "policy": self.policy,
                "task_rates": task_rates,
                "queued": queued,
                "utilization": utilization,
                "selected": self.plan,
                "candidates": candidates,
                "reason": "selected" if selected else "no_trusted_feasible_plan",
                "cost_basis": "predicted",
                "slo_guarantee": False,
            }
            self._log("control_decision", **self.last_report)
            if selected and apply:
                for key, target in self.plan["targets"].items():
                    self._submit(key, target, now)
            return copy.deepcopy(self.last_report)

    def observe_arrival(self, request_id: str, task: str, now_ns: int) -> None:
        """Count offered traffic before harness admission, including queue rejection."""
        count(now_ns)
        if not request_id or not task:
            raise ValueError("Arrival identity and task are required")
        with self.router.lock:
            if request_id not in self.offered_ids:
                self.arrivals.append((now_ns, task))
                self.offered_ids.add(request_id)

    def decide(
        self,
        request_id: str,
        task: str,
        load_rps: float,
        *,
        as_of_ns=None,
        shadow=False,
        pool_load_rps=None,
        arrival_ns=None,
    ) -> dict:
        """Apply the current split, then recheck Stage 4 risk at actual capacity/load."""
        now = time.time_ns() if as_of_ns is None else as_of_ns
        with self.router.lock:
            if request_id in self.decisions:
                return copy.deepcopy(self.decisions[request_id])
            self.observe_arrival(
                request_id, task, now if arrival_ns is None else arrival_ns
            )
            cutoff = now - self.observation_s * 1e9
            while self.arrivals and self.arrivals[0][0] < cutoff:
                self.arrivals.popleft()
            while self.routed and self.routed[0][0] < cutoff:
                self.routed.popleft()
            counts = Counter(k for _, k in self.routed)
            rates = {
                k: max(
                    counts[k] / self.observation_s,
                    load_rps * (self.plan["weights"][k] if self.plan else 1),
                )
                for k in self.pools
            }
            if pool_load_rps is not None:
                if set(pool_load_rps) != set(rates) or any(
                    not number(r) for r in pool_load_rps.values()
                ):
                    raise ValueError("Invalid pool load snapshot")
                rates = {k: max(rates[k], pool_load_rps[k]) for k in rates}
            allowed = set()
            if (
                self.plan
                and self.last_tick is not None
                and now - self.last_tick <= self.period_s * 2e9
            ):
                for key, weight in self.plan["weights"].items():
                    self.deficit[key] = self.deficit[key] + weight if weight else 0
                key = max(
                    (k for k, w in self.plan["weights"].items() if w),
                    key=lambda k: (self.deficit[k], k),
                )
                self.deficit[key] -= 1
                state = self.snapshots.get(key)
                if (
                    self.plan["weights"][key]
                    and key not in self.draining
                    and key not in self.pending
                    and state is not None
                    and state.effective == self.plan["targets"][key]
                ):
                    allowed.add(key)
            decision = self.router.decide(
                request_id,
                task,
                load_rps,
                as_of_ns=now,
                shadow=shadow,
                pool_load_rps=rates,
                allowed_pools=allowed,
                arrival_ns=arrival_ns,
            )
            if decision["selected"] and not shadow:
                self.routed.append((now, decision["selected"]))
            return decision

    def observed_rates(self, *, now_ns=None) -> dict:
        """Use offered arrivals, including rejected requests, in the trailing window."""
        now = time.time_ns() if now_ns is None else now_ns
        with self.router.lock:
            return {
                t: n / self.observation_s
                for t, n in Counter(
                    t
                    for stamp, t in self.arrivals
                    if now - self.observation_s * 1e9 <= stamp <= now
                ).items()
            }
