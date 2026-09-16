from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Any


def run_parallel(items: Iterable[Any], worker: Callable[[Any], Any], *, max_workers: int = 1) -> list[Any]:
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    values = list(items)
    if not values:
        return []
    # Preserve input order while still isolating each worker's result.
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="atsuite-worker") as pool:
        futures = [pool.submit(worker, value) for value in values]
        return [future.result() for future in futures]
