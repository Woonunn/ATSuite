from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Budget:
    max_candidates: int | None = None
    max_builds: int | None = None
    max_trials: int | None = None
    max_wall_time_seconds: int | None = None
    max_estimated_spend: float | None = None
    reserved_spend: float = 0.0
    candidates: int = 0
    builds: int = 0
    trials: int = 0

    def reserve(self, *, candidates: int = 0, builds: int = 0, trials: int = 0, spend: float = 0.0) -> None:
        if self.max_candidates is not None and self.candidates + candidates > self.max_candidates:
            raise RuntimeError("candidate budget exceeded")
        if self.max_builds is not None and self.builds + builds > self.max_builds:
            raise RuntimeError("build budget exceeded")
        if self.max_trials is not None and self.trials + trials > self.max_trials:
            raise RuntimeError("trial budget exceeded")
        if self.max_estimated_spend is not None and self.reserved_spend + spend > self.max_estimated_spend:
            raise RuntimeError("estimated spend budget exceeded")
        self.candidates += candidates
        self.builds += builds
        self.trials += trials
        self.reserved_spend += spend
