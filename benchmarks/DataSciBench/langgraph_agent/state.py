"""Only JSON control data and references belong in LangGraph state."""
from typing import Annotated, TypedDict
from operator import add


def merge_results(left, right):
    if left.keys() & right.keys():
        raise ValueError('Duplicate parallel result key')
    return {**left, **right}


class AgentState(TypedDict, total=False):
    request_id: str
    session_id: str
    task_id: str
    user_prompt: str
    messages: Annotated[list[dict], add]
    workspace_id: str
    dataset_refs: list[dict]
    execution_plan: list[dict]
    tool_results: Annotated[dict, merge_results]
    artifacts: list[dict]
    iteration: int
    status: str
    error: dict | None
    snapshot: int
    completed: list[str]
    step: dict
    feedback: str
    validation: dict

    evaluation_basis: dict
