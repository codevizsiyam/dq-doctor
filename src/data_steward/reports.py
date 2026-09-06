from __future__ import annotations

from data_steward.models import ActivityReport, IncidentStatus
from data_steward.repositories import Repository

INVESTIGATION_ACTIONS = {"investigation_completed", "recommendation_created"}
CHANGED_ACTIONS = {"remediation_applied"}


def build_activity_report(repository: Repository) -> ActivityReport:
    incidents = repository.list_incidents()
    audit = repository.get_audit()
    investigated = {event.incident_id for event in audit if event.action in INVESTIGATION_ACTIONS}
    changed = {event.incident_id for event in audit if event.action in CHANGED_ACTIONS}
    by_status = {incident.status: 0 for incident in incidents}
    for incident in incidents:
        by_status[incident.status] = by_status.get(incident.status, 0) + 1
    return ActivityReport(
        incidents=len(incidents),
        investigated=len(investigated),
        escalated=sum(1 for incident in incidents if incident.status == IncidentStatus.ESCALATED),
        approved=len(
            [
                approval
                for approval in repository.list_approvals()
                if approval.decision == "approved"
            ]
        ),
        rejected=sum(1 for incident in incidents if incident.status == IncidentStatus.REJECTED),
        acknowledged=sum(
            1 for incident in incidents if incident.status == IncidentStatus.ACKNOWLEDGED
        ),
        changed_records=len(changed),
        unauthorized_modifications=sum(
            1 for event in audit if event.action == "unauthorized_write_blocked"
        ),
    )
