from __future__ import annotations

import random
from dataclasses import dataclass, asdict
from typing import Iterable

from atsuite.recommender.identity import run_id


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    name: str
    input_ref: str = ""
    session_policy: str = "isolated"
    offered_load: float = 1.0
    skip_sleep: bool = False


@dataclass(frozen=True)
class TrialAssignment:
    experiment_id: str
    candidate_id: str
    scenario_id: str
    block_id: str
    index: int
    run_id: str
    phase: str


@dataclass(frozen=True)
class ExperimentProtocol:
    experiment_id: str
    scenarios: tuple[Scenario, ...]
    warmup_runs: int = 2
    exploration_runs: int = 5
    confirmation_runs: int = 300
    seed: int = 0
    confidence_level: float = 0.95
    include_failures: bool = True

    def schedule(self, candidate_ids: Iterable[str], *, phase: str = "confirmation") -> list[TrialAssignment]:
        candidates = sorted(set(candidate_ids))
        count = {"warmup": self.warmup_runs, "exploration": self.exploration_runs, "confirmation": self.confirmation_runs}.get(phase)
        if count is None:
            raise ValueError(f"unknown phase: {phase}")
        rng = random.Random(self.seed)
        rows = []
        for scenario in sorted(self.scenarios, key=lambda item: item.scenario_id):
            order = list(candidates)
            for block in range(count):
                rng.shuffle(order)
                for index, candidate_id in enumerate(order):
                    rows.append(TrialAssignment(self.experiment_id, candidate_id, scenario.scenario_id, f"{phase}-{block}", index,
                                                 run_id(self.experiment_id, candidate_id, scenario.scenario_id, f"{phase}-{block}", index), phase))
        return rows

    def to_dict(self) -> dict:
        return {"experiment_id": self.experiment_id, "scenarios": [asdict(scenario) for scenario in self.scenarios],
                "warmup_runs": self.warmup_runs, "exploration_runs": self.exploration_runs,
                "confirmation_runs": self.confirmation_runs, "seed": self.seed, "confidence_level": self.confidence_level,
                "include_failures": self.include_failures}
