from __future__ import annotations

import importlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from atsuite.execution.jobs import ExperimentJob
from atsuite.execution.store import JobStore
from atsuite.execution.postgres_store import open_store


HandlerFactory = Callable[[JobStore, str], Mapping[str, Callable[[dict[str, Any]], dict[str, Any] | None]]]


def load_handlers(spec: str, store: JobStore, experiment_id: str) -> dict[str, Callable]:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("handler spec must be module:factory")
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise ValueError(f"handler factory is not callable: {spec}")
    handlers = factory(store, experiment_id)
    if not isinstance(handlers, Mapping):
        raise ValueError("handler factory must return a mapping")
    return dict(handlers)


def run_once(*, store_path: str | Path, experiment_id: str, handlers: Mapping[str, Callable]) -> dict[str, Any]:
    store = open_store(store_path)
    try:
        return ExperimentJob(store, experiment_id, dict(handlers)).run()
    finally:
        store.close()
