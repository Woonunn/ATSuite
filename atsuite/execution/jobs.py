from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from atsuite.recommender.search import SearchOrchestrator


@dataclass
class ExperimentJob:
    """Durable stage driver; stage handlers own cloud work and artifact writes."""

    store: Any
    experiment_id: str
    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]]

    def _finish_with_cleanup(
        self,
        orchestrator: SearchOrchestrator,
        terminal_state: str,
        *,
        reason: str,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self.store.get(self.experiment_id)
        terminal_updates = dict(updates or {})
        if record["state"] != "CLEANUP_PENDING":
            record = orchestrator.transition(
                self.experiment_id,
                "CLEANUP_PENDING",
                cleanup_reason=reason,
                cleanup_terminal_state=terminal_state,
            )

        try:
            if record.get("keep_resources"):
                cleanup_updates = {"cleanup_status": "kept"}
            else:
                handler = self.handlers.get("CLEANUP")
                cleanup_required = bool(
                    record.get("cleanup_required")
                    or record.get("deployment_records")
                )
                if handler is None and cleanup_required:
                    raise RuntimeError("cleanup handler is required for deployed resources")
                cleanup_updates = (handler(record) if handler else {}) or {}
                cleanup_updates.setdefault(
                    "cleanup_status", "success" if handler else "not_needed"
                )
            terminal_updates.update(cleanup_updates)
            return orchestrator.transition(
                self.experiment_id, terminal_state, **terminal_updates
            )
        except Exception as exc:
            terminal_updates.update(
                cleanup_status="failed",
                cleanup_error=f"{type(exc).__name__}: {exc}",
            )
            failed_state = terminal_state if terminal_state in {"FAILED", "CANCELLED"} else "FAILED"
            if failed_state == "FAILED" and "error" not in terminal_updates:
                terminal_updates["error"] = "cleanup failed before completion"
            return orchestrator.transition(
                self.experiment_id, failed_state, **terminal_updates
            )

    def run(self) -> dict[str, Any]:
        orchestrator = SearchOrchestrator(self.store)
        record = self.store.get(self.experiment_id)
        if record["state"] == "CLEANUP_PENDING":
            return self._finish_with_cleanup(
                orchestrator,
                str(record.get("cleanup_terminal_state") or "FAILED"),
                reason=str(record.get("cleanup_reason") or "resume"),
            )

        stages = ("BUILDING", "DEPLOYING", "EXPLORING", "CONFIRMING", "ANALYZING", "COMPLETED")
        try:
            for stage in stages:
                record = self.store.get(self.experiment_id)
                if record["state"] == "CANCEL_REQUESTED":
                    return self._finish_with_cleanup(
                        orchestrator, "CANCELLED", reason="cancelled", updates={"cancelled": True}
                    )
                if stage == "COMPLETED":
                    return self._finish_with_cleanup(
                        orchestrator, "COMPLETED", reason="completed"
                    )
                record = orchestrator.transition(self.experiment_id, stage)
                handler = self.handlers.get(stage)
                if handler is None:
                    raise RuntimeError(f"missing execution handler for stage {stage}")
                updates = handler(record) or {}
                if updates:
                    record = self.store.transition(self.experiment_id, stage, **updates)
            return record
        except Exception as exc:
            current = self.store.get(self.experiment_id)
            if current["state"] in {"COMPLETED", "CANCELLED", "FAILED"}:
                raise
            return self._finish_with_cleanup(
                orchestrator,
                "FAILED",
                reason="failed",
                updates={"error": f"{type(exc).__name__}: {exc}"},
            )
