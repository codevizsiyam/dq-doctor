from __future__ import annotations

from typing import Any

from data_steward.models import IncidentStatus
from data_steward.workflow.toolkit import InvestigationToolkit

QUEUE_READ_TOOLS = ("lookup_customer", "compare_records")


def run_queue_lookups(
    toolkit: InvestigationToolkit,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Bounded read-only loop over open incidents. Never applies a golden write."""
    incidents = [
        incident
        for incident in toolkit.repository.list_incidents()
        if incident.status == IncidentStatus.OPEN
    ][:limit]
    rows: list[dict[str, Any]] = []
    errors = 0
    unauthorized = 0
    for incident in incidents:
        row_error = 0
        row_unauthorized = 0
        payload, step, ok = toolkit.execute(
            "lookup_customer",
            {"customer_id": incident.customer_id or ""},
        )
        tools = ["lookup_customer"]
        if not ok or not step.authorized:
            row_unauthorized += 1
        if isinstance(payload, dict) and payload.get("error"):
            row_error += 1
        if incident.customer_id:
            compared, compare_step, compare_ok = toolkit.execute(
                "compare_records",
                {"customer_id": incident.customer_id},
            )
            tools.append("compare_records")
            if not compare_ok or not compare_step.authorized:
                row_unauthorized += 1
            if isinstance(compared, dict) and compared.get("error"):
                row_error += 1
        errors += row_error
        unauthorized += row_unauthorized
        rows.append(
            {
                "incident_id": incident.incident_id,
                "customer_id": incident.customer_id,
                "tools": tools,
                "records": len(payload) if isinstance(payload, list) else 0,
                "authorized": row_unauthorized == 0 and row_error == 0,
            }
        )
    return {
        "incidents": len(rows),
        "errors": errors,
        "unauthorized_tools": unauthorized,
        "writes": 0,
        "tools": list(QUEUE_READ_TOOLS),
        "rows": rows,
    }
