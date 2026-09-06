from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class SourceSystem(StrEnum):
    CRM = "crm"
    BILLING = "billing"
    SUPPORT = "support"
    GOLDEN = "golden"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    ACKNOWLEDGED = "acknowledged"


class Decision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class SourceRecord(StrictModel):
    record_id: str
    customer_id: str
    source: SourceSystem
    data: dict[str, Any]
    observed_at: datetime


class ContractField(StrictModel):
    type: str
    nullable: bool = True
    unique: bool = False
    format: str | None = None
    classification: str | None = None
    description: str | None = None
    allowed_values: list[Any] | None = None


class GovernanceRules(StrictModel):
    pii_changes_require_approval: bool = True
    destructive_changes_require_approval: bool = True
    contract_changes_require_approval: bool = True
    mandatory_approval_fields: list[str] = Field(default_factory=list)


class DataContract(StrictModel):
    entity: str
    version: str
    owner: str
    fields: dict[str, ContractField]
    source_authority: dict[str, list[SourceSystem]] = Field(default_factory=dict)
    freshness_hours: int | None = None
    governance: GovernanceRules


class Evidence(StrictModel):
    evidence_id: str
    incident_id: str
    source: SourceSystem
    record_id: str | None = None
    field: str | None = None
    value: Any = None
    observed_at: datetime
    rationale: str


class Incident(StrictModel):
    incident_id: str
    entity: str = "customer"
    customer_id: str | None = None
    incident_type: str
    affected_fields: list[str]
    source_systems: list[SourceSystem]
    violations: list[str]
    severity: Severity
    contract_version: str
    status: IncidentStatus = IncidentStatus.OPEN
    created_at: datetime = Field(default_factory=utc_now)


# OpenAI structured outputs require every property to declare a JSON Schema
# "type". Bare typing.Any omits that and triggers BadRequestError on parse().
JsonPrimitive = str | int | float | bool | None


class FieldChange(StrictModel):
    field: str
    old_value: JsonPrimitive = None
    new_value: JsonPrimitive = None


class Proposal(StrictModel):
    proposal_id: str
    incident_id: str
    target_record_id: str
    changes: list[FieldChange]
    confidence: float = Field(ge=0, le=1)
    rationale: str
    evidence_refs: list[str] = Field(default_factory=list)
    uncertainty: str = ""
    impact: str = ""
    destructive: bool = False
    contract_change: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("changes")
    @classmethod
    def changes_are_unique(cls, changes: list[FieldChange]) -> list[FieldChange]:
        fields = [change.field for change in changes]
        if not changes or len(fields) != len(set(fields)):
            raise ValueError("proposal must contain unique field changes")
        return changes


class Approval(StrictModel):
    approval_id: str
    proposal_id: str
    decision: Decision
    reviewer: str
    comment: str | None = None
    approved_changes: list[FieldChange] | None = None
    decided_at: datetime = Field(default_factory=utc_now)


class ValidationResult(StrictModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GovernanceAssessment(StrictModel):
    approval_required: bool
    reasons: list[str]
    risk: str


class RecordComparison(StrictModel):
    customer_id: str
    values_by_field: dict[str, dict[str, Any]]
    conflicting_fields: list[str]


class AddressValidation(StrictModel):
    valid: bool
    normalized: dict[str, str]
    errors: list[str] = Field(default_factory=list)


class SchemaChange(StrictModel):
    kind: str
    field: str
    old_value: Any = None
    new_value: Any = None
    breaking: bool


class LineageEdge(StrictModel):
    lineage_id: str
    source_system: SourceSystem
    source_record_id: str
    target_system: SourceSystem = SourceSystem.GOLDEN
    target_record_id: str
    fields: list[str]
    recorded_at: datetime = Field(default_factory=utc_now)


class AuditEvent(StrictModel):
    event_id: str
    incident_id: str
    proposal_id: str | None = None
    action: str
    actor: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    verification_succeeded: bool | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class InvestigationStep(StrictModel):
    step: int
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: str
    authorized: bool = True


class Recommendation(StrictModel):
    outcome: Literal["recommend", "escalate", "report_only"]
    incident_id: str
    classified_type: str
    target_record_id: str = ""
    changes: list[FieldChange] = Field(default_factory=list)
    candidate_value: dict[str, JsonPrimitive] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    uncertainty: str
    impact: str
    rationale: str
    tools_used: list[str] = Field(default_factory=list)
    fallback_used: bool = False

    def to_proposal(self, proposal_id: str) -> Proposal | None:
        if self.outcome != "recommend" or not self.changes or not self.target_record_id:
            return None
        return Proposal(
            proposal_id=proposal_id,
            incident_id=self.incident_id,
            target_record_id=self.target_record_id,
            changes=self.changes,
            confidence=self.confidence,
            rationale=self.rationale,
            evidence_refs=self.evidence_refs,
            uncertainty=self.uncertainty,
            impact=self.impact,
            contract_change=self.outcome == "report_only",
        )


class HumanDecision(StrictModel):
    action: Literal["approve", "edit", "reject", "acknowledge"]
    reviewer: str
    comment: str = ""
    edited_changes: list[FieldChange] = Field(default_factory=list)


class ActivityReport(StrictModel):
    incidents: int
    investigated: int
    escalated: int
    approved: int
    rejected: int
    acknowledged: int
    changed_records: int
    unauthorized_modifications: int = 0


class RemediationResult(StrictModel):
    applied: bool
    idempotent_replay: bool
    verification: ValidationResult
    record: SourceRecord
