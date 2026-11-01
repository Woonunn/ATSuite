"""Seeded Tool-level discrete-event replay; historical E2E times are never replayed."""

import copy
import heapq
import itertools
import json
import random
from collections import Counter, defaultdict, deque
from pathlib import Path

from atsuite.provider_model.model import _distribution
from atsuite.provider_model.observation import validate_pool
from atsuite.routing.capacity import Capacity, count
from atsuite.provider_model.analytical import pool_cost, predict_cost
from atsuite.tracing.storage import read_text
from atsuite.workload.profile import number


def load_sealed_workload(directory: Path) -> tuple[list, dict, dict]:
    """Reuse sealed Copilot arrivals and Capture ATGs, discarding every Tool duration."""
    from experiments.exp1.data.prepare_e1b import ROOT, load, sha

    manifest, rows = load(directory)
    if len({(r.get("scenario"), r.get("window_role")) for r in rows}) != 1:
        raise ValueError(
            "Select one sealed scenario/window; overlapping windows cannot be combined"
        )
    arrivals = [
        {
            "id": r["arrival_id"],
            "task_id": r["task_id"],
            "at_s": float(r["relative_arrival_ms"]) / 1000,
        }
        for r in rows
    ]
    workflows = {}
    for task, entry in manifest["tasks"].items():
        atg = json.loads(read_text(ROOT / entry["capture"] / "trace.atg.json"))
        nodes, edges = [], []
        for node in atg["nodes"]:
            if node["type"] not in {"start", "end", "llm", "tool_use"}:
                raise ValueError("Unsupported sealed ATG node")
            nodes.append(
                {
                    "id": str(node["id"]),
                    "name": node["name"],
                    "sample_key": str(node["id"]) + ":" + node["name"],
                    "kind": "tool" if node["type"] == "tool_use" else "llm",
                    "wait_s": node["time"] / 1000 if node["type"] == "llm" else 0,
                }
            )
            edges.extend([str(node["id"]), str(e["id"])] for e in node["edge_to"])
        workflows[task] = {"nodes": nodes, "edges": edges}
    return (
        arrivals,
        workflows,
        {
            "window_duration_s": manifest["config"]["window_duration_ms"] / 1000,
            "manifest_sha256": sha(directory / "manifest.json"),
            "request_table_sha256": manifest["request_table_sha256"],
            "task_capture_hashes": {
                t: e["capture_files"] for t, e in manifest["tasks"].items()
            },
        },
    )


def stratum(row: dict) -> tuple:
    """Exact measured configuration/load/concurrency/Tool identity."""
    return (
        json.dumps(row["pool"], sort_keys=True),
        row["task_id"],
        row["tool"],
        row["load_rps"],
        row["concurrent_sessions"],
    )


class ServiceSurface:
    """Empirical service samples, excluding queue time, with independent validation.

    Every Tool row includes execution + state synchronization, separately measured
    cold-start delay, and status. Unknown timing components block the simulator.
    Source runs, request IDs and sample IDs must be disjoint across train/validation.
    """

    def __init__(
        self,
        training: list[dict],
        validation: list[dict],
        *,
        min_samples=10,
        max_relative_p95_error=0.3,
    ):
        count(min_samples)
        if min_samples < 1 or not number(max_relative_p95_error):
            raise ValueError("Invalid simulator validation thresholds")
        self.groups = defaultdict(list)
        self.synthetic = any(
            r.get("basis") != "measured" for r in training + validation
        )
        for rows in (training, validation):
            if len({r["id"] for r in rows}) != len(rows):
                raise ValueError("Duplicate Tool sample")
            for row in rows:
                validate_pool(row["pool"])
                count(row["concurrent_sessions"])
                if (
                    not row.get("source_run")
                    or not row.get("request_id")
                    or not row.get("evidence")
                    or not row.get("task_id")
                    or not row.get("tool")
                    or any(
                        not number(row.get(k))
                        for k in ("service_s", "cold_start_s", "load_rps")
                    )
                    or row.get("status") not in {"success", "failed"}
                    or row.get("queue_excluded") is not True
                ):
                    raise ValueError(
                        "Missing independently measured service/cold/queue evidence"
                    )
        for key in ("id", "request_id", "source_run"):
            if {r[key] for r in training} & {r[key] for r in validation}:
                raise ValueError("Simulator validation leaks training " + key)
        held = defaultdict(list)
        for row in training:
            self.groups[stratum(row)].append(copy.deepcopy(row))
        for row in validation:
            held[stratum(row)].append(row)
        self.validation = []
        self.validated = set()
        for key in sorted(set(self.groups) | set(held)):
            train, test = self.groups[key], held[key]
            estimate = _distribution(
                [r["service_s"] + r["cold_start_s"] for r in train], len(train)
            )["p95"]
            observed = _distribution(
                [r["service_s"] + r["cold_start_s"] for r in test], len(test)
            )["p95"]
            error = (
                abs(estimate - observed) / max(observed, 1e-9)
                if estimate is not None and observed is not None
                else None
            )
            passed = (
                min(len(train), len(test)) >= min_samples
                and error is not None
                and error <= max_relative_p95_error
            )
            if passed:
                self.validated.add(key)
            self.validation.append(
                {
                    "stratum": key,
                    "training": len(train),
                    "held_out": len(test),
                    "predicted_p95_s": estimate,
                    "observed_p95_s": observed,
                    "relative_p95_error": error,
                    "passed": passed,
                }
            )

    def sample(
        self,
        pool: dict,
        task: str,
        tool: str,
        load_rps: float,
        concurrent_sessions: int,
        rng: random.Random,
    ) -> dict:
        """Sample only a validated measured load envelope and exact concurrency."""
        query = (json.dumps(pool, sort_keys=True), task, tool)
        candidates = [
            key
            for key in self.validated
            if key[:3] == query
            and load_rps <= key[3] <= max(load_rps * 1.25, load_rps + 0.01)
            and key[4] == concurrent_sessions
        ]
        if not candidates:
            raise ValueError("No independently validated Tool/load/capacity stratum")
        key = min(candidates, key=lambda k: k[3])
        return copy.deepcopy(rng.choice(self.groups[key]))


class SimulatedAdapter:
    """Explicit simulator capacity, never reported as cloud readback."""

    def __init__(self, control: str, initial: int | None, delays: dict):
        self.control, self.target, self.effective = control, initial, initial
        self.delays, self.now, self.ready_at = delays, 0, 0

    def read(self, now_ns=None) -> Capacity:
        self.now = self.now if now_ns is None else now_ns
        if self.now >= self.ready_at:
            self.effective = self.target
        return Capacity(
            self.now,
            self.control,
            self.target,
            self.effective,
            status="READY" if self.now >= self.ready_at else "IN_PROGRESS",
        )

    def apply(self, target: int, *, active_sessions: int) -> None:
        count(target)
        if self.control == "managed":
            raise ValueError("Managed runtime cannot be assigned simulated instances")
        if target < self.target and active_sessions:
            raise ValueError("Active simulated Sessions prevent shrink")
        self.target = target
        self.ready_at = self.now + int(self.delays[target] * 1e9)


def simulate(
    controller,
    arrivals: list[dict],
    workflows: dict,
    surface: ServiceSurface,
    *,
    seed: int,
    end_s: float,
    start_ns=10**18,
) -> dict:
    """Replay a fixed request table with resampled Tool work, waits, queues and cold starts.

    Workflows contain nodes {id, kind: tool|llm, name, wait_s (LLM only)} and edges.
    Session capacity and Tool invocation slots are distinct calibrated quantities.
    All offered arrivals remain in the result, including admission rejections.
    """
    if not number(end_s) or end_s == 0 or not arrivals:
        raise ValueError("A finite run deadline and arrivals are required")
    if len({r["id"] for r in arrivals}) != len(arrivals):
        raise ValueError("Duplicate arrival IDs")
    if any(not isinstance(a, SimulatedAdapter) for a in controller.adapters.values()):
        raise ValueError("The simulator cannot use cloud adapters")
    for graph in workflows.values():
        ids = [n["id"] for n in graph["nodes"]]
        if not ids or len(ids) != len(set(ids)) or "__session__" in ids:
            raise ValueError("Invalid workflow nodes")
        remaining = set(ids)
        for node in graph["nodes"]:
            if node["kind"] not in {"tool", "llm"} or (
                node["kind"] == "llm" and not number(node.get("wait_s"))
            ):
                raise ValueError("Invalid workflow duration/kind")
        if any(a not in remaining or b not in remaining for a, b in graph["edges"]):
            raise ValueError("Unknown dependency")
        while remaining:
            ready = {
                n
                for n in remaining
                if not any(b == n and a in remaining for a, b in graph["edges"])
            }
            if not ready:
                raise ValueError("Workflow contains a cycle")
            remaining -= ready
    rng, sequence, events = random.Random(seed), itertools.count(), []
    records, jobs, waiting, busy = {}, {}, defaultdict(deque), Counter()
    costs, occupied_seconds, capacity_seconds = Counter(), Counter(), Counter()
    unsupported, scaling_events, cost_missing = [], [], []
    cost_timeline, capacity_timeline = [], []
    end_ns = start_ns + int(end_s * 1e9)

    def schedule(when, kind, value):
        heapq.heappush(events, (when, next(sequence), kind, value))

    for row in arrivals:
        if (
            not number(row["at_s"])
            or row["at_s"] > end_s
            or row["task_id"] not in workflows
        ):
            raise ValueError("Arrival outside the run or missing workflow")
        schedule(start_ns + int(row["at_s"] * 1e9), "arrival", row)
    schedule(start_ns, "tick", None)
    previous = start_ns
    tick_count = 0

    def usage(until):
        nonlocal previous
        seconds = (until - previous) / 1e9
        for key, state in controller.snapshots.items():
            target = (
                state.effective
                if state.effective in controller.levels[key]
                else state.target
            )
            level = controller.levels[key].get(target)
            if level is None:
                raise ValueError("Unknown simulated capacity cost")
            active = controller._occupied(key)
            card = level["pool"].get(
                "billing",
                controller.router.provider.tariffs.get(level["pool"]["provider"]),
            )
            cost = pool_cost(card, requests=0, marginal=None, horizon_s=seconds)
            conversion = controller.fx["rates"][card["currency"]]
            for component, amount in cost["components"].items():
                costs[component] += amount * conversion
            if key in controller.pending:
                requested = controller.levels[key][controller.pending[key]["target"]]
                requested_card = requested["pool"].get("billing", card)
                costs["scaling"] += (
                    seconds * requested_card["rates"]["pool_per_s"] * conversion
                )
            capacity_timeline.append(
                {
                    "at_s": (until - start_ns) / 1e9,
                    "pool": key,
                    "target": target,
                    "session_limit": level["session_limit"],
                }
            )
            occupied_seconds[key] += active * seconds
            capacity_seconds[key] += level["session_limit"] * seconds
        cost_timeline.append(
            {"at_s": (until - start_ns) / 1e9, "total": sum(costs.values())}
        )
        previous = until

    def finish(rid, now, status):
        job, record = jobs[rid], records[rid]
        record.update(
            status=status,
            completed_s=(now - start_ns) / 1e9,
            latency_s=(now - job["arrival_ns"]) / 1e9,
        )
        session = {
            "id": "__session__",
            "kind": "session",
            "name": "agent",
            "start_ns": job["arrival_ns"],
            "end_ns": now,
            "duration_s": (now - job["arrival_ns"]) / 1e9,
            "status": "ok" if status == "success" else "error",
            "metadata": {},
        }
        trace = {
            "schema_version": 1,
            "request_id": rid,
            "session_id": rid,
            "arrival_ns": job["arrival_ns"],
            "completed_ns": now,
            "request_status": status,
            "spans": job["spans"],
            "diagnostics": ["simulation_deadline"] if status == "incomplete" else [],
            "source": "simulation",
        }
        trace["spans"] = [session, *job["spans"]]
        observation = controller.complete(rid, trace, available_ns=now)
        pool = observation["pool"]
        card = pool.get(
            "billing", controller.router.provider.tariffs.get(pool["provider"])
        )
        marginal = predict_cost([observation["profile"]], pool, card, [], measured=True)
        if marginal["complete"]:
            for component, value in marginal["components"].items():
                costs[component] += (
                    value["mean"] * controller.fx["rates"][card["currency"]]
                )
        else:
            cost_missing.append({"request_id": rid, "missing": marginal["missing"]})
        job["finished"] = True

    def ready_nodes(rid, now):
        job = jobs[rid]
        graph = workflows[job["task"]]
        for node in graph["nodes"]:
            nid = node["id"]
            if nid in job["started"] or any(
                b == nid and a not in job["done"] for a, b in graph["edges"]
            ):
                continue
            job["started"].add(nid)
            if node["kind"] == "llm":
                schedule(
                    now + int(node["wait_s"] * 1e9), "done", (rid, node, now, True)
                )
            else:
                waiting[job["pool"]].append((rid, node, now))

    def dispatch(now):
        for key, queue in waiting.items():
            state = controller.snapshots[key]
            target = (
                state.effective
                if state.effective in controller.levels[key]
                else state.target
            )
            level = controller.levels[key][target]
            slots = level["tool_slots"]
            count(slots)
            while queue and busy[key] < slots:
                rid, node, enqueued = queue.popleft()
                job = jobs[rid]
                if job["finished"] or job["failed"]:
                    continue
                recent = sum(
                    1
                    for t, k in controller.routed
                    if k == key and t > now - controller.observation_s * 1e9
                )
                rate = recent / controller.observation_s
                try:
                    row = surface.sample(
                        level["pool"],
                        job["task"],
                        node.get("sample_key", node["name"]),
                        rate,
                        controller._occupied(key),
                        rng,
                    )
                except ValueError as exc:
                    unsupported.append(
                        {"request_id": rid, "node": node["id"], "reason": str(exc)}
                    )
                    job["failed"] = True
                    # Existing parallel invocations still own their slots until completion.
                    if not job["running"]:
                        finish(rid, now, "failed")
                    continue
                duration = row["service_s"] + row["cold_start_s"]
                job["queue_s"] += (now - enqueued) / 1e9
                job["running"] += 1
                busy[key] += 1
                schedule(
                    now + max(1, int(duration * 1e9)),
                    "done",
                    (rid, node, now, row["status"] == "success"),
                )

    while events and events[0][0] <= end_ns:
        now, _, kind, value = heapq.heappop(events)
        usage(now)
        if kind == "tick":
            rates = controller.observed_rates(now_ns=now) or {t: 0.0 for t in workflows}
            before = {k: a.target for k, a in controller.adapters.items()}
            controller.tick(rates, now_ns=now)
            for key, adapter in controller.adapters.items():
                if before[key] != adapter.target:
                    scaling_events.append(
                        {
                            "pool": key,
                            "at_s": (now - start_ns) / 1e9,
                            "target": adapter.target,
                        }
                    )
                    level = controller.levels[key][adapter.target]
                    card = level["pool"].get(
                        "billing",
                        controller.router.provider.tariffs.get(
                            level["pool"]["provider"]
                        ),
                    )
                    costs["scaling"] += (
                        card["rates"]["scale_event"]
                        * controller.fx["rates"][card["currency"]]
                    )
            tick_count += 1
            schedule(now + int(controller.period_s * 1e9), "tick", None)
        elif kind == "arrival":
            rid, task = value["id"], value["task_id"]
            load = (
                sum(controller.observed_rates(now_ns=now).values())
                + 1 / controller.observation_s
            )
            decision = controller.decide(
                rid, task, load, as_of_ns=now, descriptor=value.get("descriptor")
            )
            key = decision["selected"]
            records[rid] = {
                "id": rid,
                "task_id": task,
                "arrival_s": value["at_s"],
                "pool": key,
                "status": "active" if key else "rejected",
                "latency_s": None,
            }
            if key:
                level = controller.levels[key][controller.snapshots[key].effective]
                jobs[rid] = {
                    "task": task,
                    "pool": key,
                    "arrival_ns": now,
                    "done": set(),
                    "started": set(),
                    "spans": [],
                    "queue_s": 0,
                    "finished": False,
                    "failed": False,
                    "running": 0,
                }
                ready_nodes(rid, now)
        else:
            rid, node, started, success = value
            job = jobs[rid]
            if node["kind"] == "tool":
                busy[job["pool"]] -= 1
                job["running"] -= 1
            if job["finished"]:
                continue
            job["done"].add(node["id"])
            job["spans"].append(
                {
                    "id": node["id"],
                    "name": node["name"],
                    "kind": node["kind"],
                    "start_ns": started,
                    "end_ns": now,
                    "duration_s": (now - started) / 1e9,
                    "status": "ok" if success else "error",
                    "metadata": copy.deepcopy(node.get("metadata", {})),
                    "resources": copy.deepcopy(node.get("resources", {})),
                    "dependencies": [
                        a for a, b in workflows[job["task"]]["edges"] if b == node["id"]
                    ],
                }
            )
            if node["kind"] == "tool":
                job["spans"][-1]["metadata"]["runtime"] = {
                    "execution_elapsed_ms": (now - started) / 1e6,
                    "provider_metadata": {"state_sync": {"total_ms": 0}},
                }
            job["failed"] |= not success
            if job["failed"] and not job["running"]:
                finish(rid, now, "failed")
            elif not job["failed"] and len(job["done"]) == len(
                workflows[job["task"]]["nodes"]
            ):
                records[rid]["queue_s"] = job["queue_s"]
                finish(rid, now, "success")
            elif not job["failed"]:
                ready_nodes(rid, now)
        dispatch(now)
    usage(end_ns)
    controller.expire_retained(end_ns)
    for rid, job in jobs.items():
        if not job["finished"]:
            finish(rid, end_ns, "incomplete")
    billing_end = end_ns
    for deadline in sorted(set(controller.retained_until.values())):
        usage(deadline)
        controller.expire_retained(deadline)
        billing_end = deadline
    rows = list(records.values())
    successful = [r for r in rows if r["status"] == "success"]
    admitted = [r for r in rows if r["pool"]]
    violations = [r for r in admitted if r["status"] != "success"]
    return {
        "basis": "simulation",
        "synthetic": surface.synthetic,
        "seed": seed,
        "policy": controller.policy,
        "requests": rows,
        "offered": len(rows),
        "admitted": len(admitted),
        "success": len(successful),
        "admission_rate": len(admitted) / len(rows),
        "success_rate": len(successful) / len(rows),
        "failure_rate_admitted": len(violations) / len(admitted) if admitted else None,
        "unsatisfied_rate_offered": (len(rows) - len(admitted) + len(violations))
        / len(rows),
        "p95_s": _distribution([r["latency_s"] for r in successful], len(successful))[
            "p95"
        ],
        "cost": {
            "basis": "estimated",
            "currency": controller.fx["currency"],
            "fx": controller.fx,
            "components": dict(costs),
            "total": sum(costs.values()) if not cost_missing else None,
            "subtotal": sum(costs.values()),
            "missing": cost_missing,
            "actual": None,
        },
        "session_utilization": {
            k: occupied_seconds[k] / capacity_seconds[k]
            if capacity_seconds[k]
            else None
            for k in controller.pools
        },
        "scaling_events": scaling_events,
        "cost_timeline": cost_timeline,
        "capacity_timeline": capacity_timeline,
        "latency": _distribution([r["latency_s"] for r in successful], len(successful)),
        "control_cycles": tick_count,
        "unsupported": unsupported,
        "valid": bool(admitted) and not unsupported,
        "validation": surface.validation,
        "deadline_s": end_s,
        "billing_end_s": (billing_end - start_ns) / 1e9,
        "scope": "Simulated measured service distributions; not observed mixed-cloud latency or savings",
    }
