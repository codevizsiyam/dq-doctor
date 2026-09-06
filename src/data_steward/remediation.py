from __future__ import annotations

from data_steward.governance import validate_proposal, validate_record
from data_steward.models import (
    AuditEvent,
    DataContract,
    Decision,
    IncidentStatus,
    Proposal,
    RemediationResult,
    SourceRecord,
    SourceSystem,
)
from data_steward.repositories import Repository


class RemediationError(RuntimeError):
    pass


class RemediationService:
    """The only application service authorized to mutate governed golden records."""

    def __init__(self, repository: Repository, contract: DataContract) -> None:
        self.repository = repository
        self.contract = contract

    def apply(self, proposal_id: str) -> RemediationResult:
        proposal = self.repository.get_proposal(proposal_id)
        if proposal is None:
            raise RemediationError("proposal is not persisted")

        audit_id = f"remediation:{proposal_id}"
        prior_events = self.repository.get_audit(proposal.incident_id)
        if any(event.event_id == audit_id for event in prior_events):
            record = self._target(proposal)
            return RemediationResult(
                applied=False,
                idempotent_replay=True,
                verification=validate_record(record.data, self.contract),
                record=record,
            )

        approval = self.repository.get_approval(proposal_id)
        if approval is None or approval.decision != Decision.APPROVED:
            raise RemediationError("a persisted approved decision is required")
        effective = (
            proposal.model_copy(update={"changes": approval.approved_changes})
            if approval.approved_changes is not None
            else proposal
        )
        target = self._target(proposal)
        validation = validate_proposal(effective, target, self.contract)
        if not validation.valid:
            raise RemediationError("proposal validation failed: " + "; ".join(validation.errors))

        before = target.data.copy()
        after = before.copy()
        for change in effective.changes:
            after[change.field] = change.new_value
        changed_record = target.model_copy(update={"data": after})
        self.repository.save_record(changed_record)

        verification = validate_record(after, self.contract)
        if not verification.valid:
            self.repository.save_record(target)
            raise RemediationError(
                "post-write verification failed; change rolled back: "
                + "; ".join(verification.errors)
            )

        self.repository.append_audit(
            AuditEvent(
                event_id=audit_id,
                incident_id=proposal.incident_id,
                proposal_id=proposal_id,
                action="remediation_applied",
                actor=approval.reviewer,
                before=before,
                after=after,
                verification_succeeded=True,
                details={
                    "approval_id": approval.approval_id,
                    "approved_at": approval.decided_at.isoformat(),
                },
            )
        )
        incident = self.repository.get_incident(proposal.incident_id)
        if incident is not None:
            self.repository.save_incident(
                incident.model_copy(update={"status": IncidentStatus.RESOLVED})
            )
        return RemediationResult(
            applied=True,
            idempotent_replay=False,
            verification=verification,
            record=changed_record,
        )

    def _target(self, proposal: Proposal) -> SourceRecord:
        record = self.repository.get_record(SourceSystem.GOLDEN, proposal.target_record_id)
        if record is None:
            raise RemediationError("golden target record does not exist")
        return record
