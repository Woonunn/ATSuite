"""Provider-neutral Tool response; client invoke() keeps its legacy return value."""
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolResponse:
    provider_request_id: str = ""
    status: str = "ok"
    result: Any = None
    error: str = ""
    client_elapsed_ms: float = 0.0
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    run_id: str = ""
    execution_elapsed_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def mcp_response(payload, *, request_id="", provider_request_id="", run_id="", elapsed_ms=0.0):
    if isinstance(payload, dict) and "error" in payload:
        return ToolResponse(provider_request_id=provider_request_id or "", request_id=request_id, run_id=run_id,
                            status="error", error=str(payload["error"]), client_elapsed_ms=elapsed_ms,
                            provider_metadata={"mcp_error": payload["error"]})
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise ValueError("Missing MCP tools/call result")
    response = payload["result"]
    content = response.get("content", [])
    metadata = response.get("_meta", {}).get("atsuite", {})
    if "structuredContent" in response:
        result = response["structuredContent"]
        if metadata.get("result_envelope") and isinstance(result, dict):
            result = result.get("result")
    elif len(content) == 1 and content[0].get("type") == "text":
        result = content[0]["text"]
    else:
        result = content
    is_error = bool(response.get("isError"))
    error = " | ".join(str(item["text"]) for item in content if item.get("text")) if is_error else ""
    return ToolResponse(
        provider_request_id=provider_request_id or "", request_id=request_id,
        run_id=run_id or metadata.get("run_id", ""), status=("timeout" if metadata.get("status") == "timeout" else "error") if is_error else "ok", result=result,
        error=error or (str(response) if is_error else ""),
        client_elapsed_ms=elapsed_ms, execution_elapsed_ms=metadata.get("execution_elapsed_ms"),
        provider_metadata={"mcp_result": response, **metadata},
    )
