"""Deterministic stateful workload for output and state equivalence checks."""
from atsuite_sdk.abstract import registry
from atsuite_sdk.state import register_state_object


class Counter:
    def __init__(self):
        self.total = 0
        self.amounts = []


counter = Counter()
register_state_object('counter', counter)


@registry.tool(stateful=True)
def counter_add(amount: int) -> dict:
    """Add an integer to this trial's isolated counter and return its total."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValueError('amount must be an integer')
    counter.total += amount
    counter.amounts.append(amount)
    return {'total': counter.total}


@registry.tool(stateful=True)
def counter_snapshot() -> dict:
    """Read the restored trial state without changing its business contents."""
    return {'total': counter.total, 'amounts': list(counter.amounts)}
