from __future__ import annotations

import re
from typing import Any

from data_steward.models import (
    DataContract,
    GovernanceAssessment,
    Proposal,
    SourceRecord,
    ValidationResult,
)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ZIP_PATTERN = re.compile(r"^\d{5}(?:-\d{4})?$")
STATE_PATTERN = re.compile(r"^[A-Z]{2}$")
PYTHON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def validate_record(data: dict[str, Any], contract: DataContract) -> ValidationResult:
    errors: list[str] = []
    unknown = sorted(set(data) - set(contract.fields))
    errors.extend(f"unknown field: {field}" for field in unknown)
    for name, rule in contract.fields.items():
        value = data.get(name)
        if value is None:
            if not rule.nullable:
                errors.append(f"{name} is required")
            continue
        expected_type = PYTHON_TYPES.get(rule.type)
        if expected_type is None:
            errors.append(f"{name} has unsupported contract type {rule.type}")
            continue
        if isinstance(value, bool) and rule.type in {"integer", "number"}:
            errors.append(f"{name} must be {rule.type}")
        elif not isinstance(value, expected_type):
            errors.append(f"{name} must be {rule.type}")
        if rule.allowed_values is not None and value not in rule.allowed_values:
            errors.append(f"{name} is not an allowed value")
        if rule.format == "email" and isinstance(value, str) and not EMAIL_PATTERN.fullmatch(value):
            errors.append(f"{name} is not a valid email")
        if rule.format == "us_zip" and isinstance(value, str) and not ZIP_PATTERN.fullmatch(value):
            errors.append(f"{name} is not a valid US ZIP code")
        if rule.format == "us_state" and isinstance(value, str) and not STATE_PATTERN.fullmatch(value):
            errors.append(f"{name} is not a valid US state code")
    return ValidationResult(valid=not errors, errors=errors)


def validate_proposal(
    proposal: Proposal,
    target: SourceRecord | None,
    contract: DataContract,
) -> ValidationResult:
    if target is None:
        return ValidationResult(valid=False, errors=["target record does not exist"])
    errors: list[str] = []
    if target.record_id != proposal.target_record_id:
        errors.append("proposal target does not match loaded record")
    updated = target.data.copy()
    for change in proposal.changes:
        if change.field not in contract.fields:
            errors.append(f"field is not in active contract: {change.field}")
            continue
        if updated.get(change.field) != change.old_value:
            errors.append(f"stale old value for {change.field}")
        updated[change.field] = change.new_value
    result = validate_record(updated, contract)
    errors.extend(result.errors)
    return ValidationResult(valid=not errors, errors=errors)


def assess_governance(
    proposal: Proposal,
    contract: DataContract,
    confidence_threshold: float,
    *,
    material_evidence_conflict: bool = False,
) -> GovernanceAssessment:
    reasons: list[str] = []
    changed_fields = {change.field for change in proposal.changes}
    pii_fields = {
        name
        for name, field in contract.fields.items()
        if field.classification == "pii"
    }
    if contract.governance.pii_changes_require_approval and changed_fields & pii_fields:
        reasons.append("PII change")
    mandatory = set(contract.governance.mandatory_approval_fields)
    if changed_fields & mandatory:
        reasons.append("mandatory approval field")
    if proposal.destructive and contract.governance.destructive_changes_require_approval:
        reasons.append("destructive change")
    if proposal.contract_change and contract.governance.contract_changes_require_approval:
        reasons.append("contract change")
    if proposal.confidence < confidence_threshold:
        reasons.append("confidence below threshold")
    if material_evidence_conflict:
        reasons.append("material source conflict")
    return GovernanceAssessment(
        approval_required=bool(reasons),
        reasons=sorted(set(reasons)),
        risk="high" if reasons else "low",
    )
