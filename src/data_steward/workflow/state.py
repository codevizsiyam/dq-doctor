from typing import Any, NotRequired, TypedDict


class StewardState(TypedDict):
    incident_id: str
    incident: NotRequired[dict[str, Any]]
    classified_type: NotRequired[str]
    records: NotRequired[list[dict[str, Any]]]
    tool_trace: NotRequired[list[dict[str, Any]]]
    step_count: NotRequired[int]
    recommendation: NotRequired[dict[str, Any]]
    proposal_id: NotRequired[str | None]
    validation: NotRequired[dict[str, Any]]
    governance: NotRequired[dict[str, Any]]
    schema_changes: NotRequired[list[dict[str, Any]]]
    downstream: NotRequired[dict[str, list[str]]]
    human_decision: NotRequired[dict[str, Any]]
    remediation: NotRequired[dict[str, Any]]
    phoenix_trace_id: NotRequired[str | None]
    fallback_used: NotRequired[bool]
    status: NotRequired[str]
    error: NotRequired[str | None]
