"""Small in-process empirical workload model with causal prediction cutoffs."""

import copy
import json
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from atsuite.tracing.storage import read_text, write_json
from atsuite.workload.profile import METRICS, extract_profile, number
from atsuite.workload.descriptor import distance, validate_descriptor


def distribution(values: list, total: int) -> dict:
    """Return exact empirical samples and interpolated quantiles, without imputation."""
    values = sorted(values)

    def quantile(q):
        if not values:
            return None
        position = (len(values) - 1) * q
        lower = int(position)
        return values[lower] + (
            values[min(lower + 1, len(values) - 1)] - values[lower]
        ) * (position - lower)

    return {
        "n": len(values),
        "missing": total - len(values),
        "coverage": len(values) / total if total else 0,
        "samples": values,
        "mean": sum(values) / len(values) if values else None,
        "p10": quantile(0.1),
        "p50": quantile(0.5),
        "p90": quantile(0.9),
        "p95": quantile(0.95),
        "p99": quantile(0.99),
        "low_confidence": len(values) < 10 or len(values) < total,
    }


class WorkloadModel:
    """Resource-family empirical samples; thread-safe within one process."""

    def __init__(self, initial_profiles: list[dict] | None = None) -> None:
        # shortcut: retain exact samples in memory; use bounded summaries when volume warrants it.
        self.initial_profiles = copy.deepcopy(initial_profiles or [])
        self.samples = {}
        self.active = {}
        self.lock = threading.RLock()

    def begin(
        self, request_id: str, session_id: str, arrival_ns: int | None = None
    ) -> dict:
        """Freeze a prediction before executing a request; count distinct active sessions."""
        arrival_ns = time.time_ns() if arrival_ns is None else arrival_ns
        if not number(arrival_ns):
            raise ValueError("Invalid arrival timestamp")
        with self.lock:
            if request_id in self.active or request_id in self.samples:
                raise ValueError("Request ID already registered")
            prediction = self.predict(arrival_ns)
            self.active[request_id] = {
                "session_id": session_id,
                "arrival_ns": arrival_ns,
            }
            prediction["concurrent_sessions"] = len(
                {v["session_id"] for v in self.active.values()}
            )
            prediction["request_id"] = request_id
            return prediction

    def observe(self, trace: dict, available_ns: int | None = None) -> dict:
        """Extract and ingest a completed trace; retain failed/incomplete audits separately."""
        return self.update(extract_profile(trace), available_ns)

    def update(
        self, profile: dict, available_ns: int | None = None, *, descriptor=None
    ) -> dict:
        """Ingest once, allowing an incomplete checkpoint to later become complete."""
        available_ns = time.time_ns() if available_ns is None else available_ns
        profile = copy.deepcopy(profile)
        if descriptor is not None:
            profile["descriptor"] = copy.deepcopy(descriptor)
        if profile.get("descriptor") is not None:
            validate_descriptor(profile["descriptor"])
        if not number(available_ns):
            raise ValueError("Invalid availability timestamp")
        if profile.get("status") not in {"success", "failed", "incomplete"}:
            raise ValueError("Invalid profile status")
        for value in profile["metrics"].values():
            if value is not None and not number(value):
                raise ValueError("Invalid profile measurement")
        for key in ("arrival_ns", "completed_ns"):
            if profile.get(key) is not None and not number(profile[key]):
                raise ValueError("Invalid request timestamp")
        if (
            profile.get("arrival_ns") is not None
            and profile.get("completed_ns") is not None
            and profile["completed_ns"] < profile["arrival_ns"]
        ):
            raise ValueError("Completion precedes arrival")
        with self.lock:
            key = profile["request_id"]
            profile = copy.deepcopy(profile)
            registered = self.active.get(key, {})
            if profile.get("arrival_ns") is None:
                profile["arrival_ns"] = registered.get("arrival_ns")
            previous = self.samples.get(key)
            if profile.get("arrival_ns") is None and previous:
                profile["arrival_ns"] = previous["profile"].get("arrival_ns")
            if previous and previous["profile"]["status"] != "incomplete":
                if previous["profile"] == profile:
                    return copy.deepcopy(profile)
                raise ValueError("Conflicting completed request")
            if (
                profile.get("completed_ns") is not None
                and profile["completed_ns"] > available_ns
            ):
                raise ValueError("Observation precedes request completion")
            if (
                profile["status"] != "incomplete"
                or profile.get("completed_ns") is not None
            ):
                self.active.pop(key, None)
            self.samples[key] = {"available_ns": available_ns, "profile": profile}
            return copy.deepcopy(profile)

    def predict(self, as_of_ns: int | None = None, *, descriptor=None) -> dict:
        """Use only observations available strictly before arrival, never the new ATG."""
        as_of_ns = time.time_ns() if as_of_ns is None else as_of_ns
        if descriptor is not None:
            validate_descriptor(descriptor)
        if not number(as_of_ns):
            raise ValueError("Invalid prediction timestamp")
        with self.lock:
            eligible = [
                s["profile"]
                for s in self.samples.values()
                if s["available_ns"] < as_of_ns
                and (
                    s["profile"].get("completed_ns") is None
                    or s["profile"]["completed_ns"] < as_of_ns
                )
            ]
            samples = [p for p in eligible if p["status"] == "success"]
            basis, nearest = "global", None
            if descriptor is not None:
                ranked = sorted(
                    (
                        (
                            distance(descriptor, p["descriptor"]),
                            -self.samples[p["request_id"]]["available_ns"],
                            -i,
                            p,
                        )
                        for i, p in enumerate(samples)
                        if p.get("descriptor") is not None
                    ),
                    key=lambda item: item[:3],
                )
                nearest = ranked[0][0] if ranked else None
                similar = [p for d, _, _, p in ranked if d <= 0.5][:20]
                if similar:
                    samples, basis = similar, "resource_family"
            if not samples:
                samples, basis = copy.deepcopy(self.initial_profiles), "initial_prior"
            n = len(samples)
            metrics = {
                k: distribution(
                    [p["metrics"][k] for p in samples if number(p["metrics"].get(k))], n
                )
                for k in METRICS
            }
            names = sorted({name for p in samples for name in p["workflow"]["calls"]})
            transitions = Counter()
            routes = defaultdict(Counter)
            for p in samples:
                nodes = {s["id"]: s["name"] for s in p["workflow"]["nodes"]}
                transitions.update(
                    (nodes[a], nodes[b]) for a, b in p["workflow"]["edges"]
                )
                for route in p["workflow"]["routes"]:
                    routes[route["node"]][tuple(route["targets"])] += 1
            return {
                "as_of_ns": as_of_ns,
                "sample_count": n,
                "match_basis": basis,
                "nearest_distance": nearest,
                "descriptor": copy.deepcopy(descriptor),
                "joint_profiles": copy.deepcopy(samples),
                "status_counts": dict(Counter(p["status"] for p in eligible)),
                "low_confidence": n < 3
                or basis == "initial_prior"
                or (descriptor is not None and basis == "global")
                or any(p["source"] == "capture" for p in samples),
                "metrics": metrics,
                "workflow": {
                    "tool_calls": {
                        name: distribution(
                            [p["workflow"]["tool_calls"].get(name, 0) for p in samples],
                            n,
                        )
                        for name in sorted(
                            {
                                name
                                for p in samples
                                for name in p["workflow"]["tool_calls"]
                            }
                        )
                    },
                    "calls": {
                        name: distribution(
                            [p["workflow"]["calls"].get(name, 0) for p in samples], n
                        )
                        for name in names
                    },
                    "presence_probability": {
                        name: sum(name in p["workflow"]["calls"] for p in samples) / n
                        for name in names
                    },
                    "causal_revisits": {
                        name: distribution(
                            [
                                p["workflow"]["causal_revisits"].get(name, 0)
                                for p in samples
                            ],
                            n,
                        )
                        for name in names
                    },
                    "dependency_edges": [
                        {"source": a, "target": b, "count": count}
                        for (a, b), count in sorted(transitions.items())
                    ],
                    "branches": {
                        name: [
                            {
                                "targets": list(targets),
                                "count": count,
                                "probability": count / sum(counts.values()),
                            }
                            for targets, count in sorted(counts.items())
                        ]
                        for name, counts in routes.items()
                    },
                    "parallel_peak": distribution(
                        [
                            p["workflow"]["parallel_peak"]
                            for p in samples
                            if p["workflow"]["parallel_peak"] is not None
                        ],
                        n,
                    ),
                },
                "tool_durations": {
                    name: distribution(
                        [
                            v
                            for p in samples
                            for v in p["tool_durations"].get(name, [])
                            if number(v)
                        ],
                        sum(len(p["tool_durations"].get(name, [])) for p in samples),
                    )
                    for name in sorted(
                        {name for p in samples for name in p["tool_durations"]}
                    )
                },
            }

    def save(self, path: Path) -> None:
        """Atomically persist samples; active requests are intentionally process-local."""
        with self.lock:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            write_json(
                Path(path),
                {
                    "schema_version": 1,
                    "samples": list(self.samples.values()),
                    "initial_profiles": self.initial_profiles,
                },
            )

    @classmethod
    def load(cls, path: Path) -> "WorkloadModel":
        """Load a model snapshot without changing observation availability times."""
        payload = json.loads(read_text(Path(path)))
        if payload.get("schema_version") != 1:
            raise ValueError("Unsupported workload model schema")
        model = cls(payload.get("initial_profiles"))
        for sample in payload["samples"]:
            model.update(sample["profile"], sample["available_ns"])
        return model
