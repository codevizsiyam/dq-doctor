from __future__ import annotations

import json
from typing import Any, Literal

from data_steward.models import Incident, Recommendation, StrictModel
from data_steward.tools import distinct_valid_operational_streets

COUNTRY_ALIASES = {"us", "ca", "gb"}


class RubricCheck(StrictModel):
    name: str
    passed: bool
    severity: Literal["fail", "warn"] = "fail"
    detail: str = ""


class RubricResult(StrictModel):
    verdict: Literal["satisfied", "needs_revision"]
    checks: list[RubricCheck]
    reasons: list[str] = []
    warnings: list[str] = []


def candidate_values_grounded(recommendation: Recommendation, records: list[Any]) -> bool:
    """Every candidate_value and change new_value appears in retrieved records."""
    if recommendation.outcome == "report_only":
        return True
    values = [str(value).lower() for value in recommendation.candidate_value.values()]
    values.extend(str(change.new_value).lower() for change in recommendation.changes)
    values = [value for value in values if value]
    if not values:
        return True
    blob = json.dumps(
        [record.model_dump(mode="json") if hasattr(record, "model_dump") else record for record in records],
        default=str,
    ).lower()
    return all(value in blob or value in COUNTRY_ALIASES for value in values)


def grade_recommendation(
    incident: Incident,
    recommendation: Recommendation,
    records: list[Any],
) -> RubricResult:
    """Deterministic pre-HITL grade. Never uses an LLM and never authorizes a write."""
    checks: list[RubricCheck] = []

    grounded = candidate_values_grounded(recommendation, records)
    ungrounded = _ungrounded_values(recommendation, records) if not grounded else []
    checks.append(
        RubricCheck(
            name="grounded_values",
            passed=grounded,
            detail=(
                "all candidate and change values appear in retrieved records"
                if grounded
                else "ungrounded values: " + ", ".join(ungrounded)
            ),
        )
    )

    schema_like = incident.incident_type == "schema_drift" or recommendation.outcome == "report_only"
    writes_ok = not (schema_like and recommendation.changes)
    checks.append(
        RubricCheck(
            name="schema_no_writes",
            passed=writes_ok,
            detail=(
                "schema/report_only has no golden-record writes"
                if writes_ok
                else "schema/report_only proposed golden-record writes"
            ),
        )
    )

    streets = distinct_valid_operational_streets(records) if incident.incident_type == "address_discrepancy" else set()
    street_ok = not (len(streets) >= 3 and recommendation.outcome == "recommend")
    checks.append(
        RubricCheck(
            name="three_street_escalate",
            passed=street_ok,
            detail=(
                f"{len(streets)} distinct valid streets; outcome={recommendation.outcome}"
                if incident.incident_type == "address_discrepancy"
                else "not an address incident"
            ),
        )
    )

    refs_check, ref_warnings = _evidence_ref_check(recommendation)
    checks.append(refs_check)

    failures = [check for check in checks if not check.passed and check.severity == "fail"]
    warnings = list(ref_warnings)
    warnings.extend(check.detail for check in checks if not check.passed and check.severity == "warn")
    return RubricResult(
        verdict="needs_revision" if failures else "satisfied",
        checks=checks,
        reasons=[check.detail for check in failures],
        warnings=warnings,
    )


def _ungrounded_values(recommendation: Recommendation, records: list[Any]) -> list[str]:
    blob = json.dumps(
        [record.model_dump(mode="json") if hasattr(record, "model_dump") else record for record in records],
        default=str,
    ).lower()
    found: list[str] = []
    for value in list(recommendation.candidate_value.values()) + [
        change.new_value for change in recommendation.changes
    ]:
        text = str(value)
        if text and text.lower() not in blob and text.lower() not in COUNTRY_ALIASES:
            found.append(text)
    return found


def _evidence_ref_check(recommendation: Recommendation) -> tuple[RubricCheck, list[str]]:
    refs = [str(ref) for ref in recommendation.evidence_refs]
    warnings: list[str] = []
    if recommendation.outcome == "report_only":
        if not refs:
            return (
                RubricCheck(
                    name="evidence_refs",
                    passed=True,
                    severity="warn",
                    detail="report_only has no evidence refs",
                ),
                ["report_only has no evidence refs"],
            )
        malformed = [ref for ref in refs if ":" not in ref]
        if malformed:
            warnings = [f"malformed evidence ref: {ref}" for ref in malformed]
            return (
                RubricCheck(
                    name="evidence_refs",
                    passed=True,
                    severity="warn",
                    detail="; ".join(warnings),
                ),
                warnings,
            )
        return (
            RubricCheck(name="evidence_refs", passed=True, detail="schema evidence refs present"),
            [],
        )

    if not refs:
        return (
            RubricCheck(
                name="evidence_refs",
                passed=False,
                detail="missing evidence refs",
            ),
            [],
        )

    three_part = []
    malformed = []
    for ref in refs:
        parts = [part for part in ref.split(":") if part]
        if len(parts) >= 3:
            three_part.append(ref)
        elif len(parts) < 2:
            malformed.append(ref)
        else:
            warnings.append(f"evidence ref is not source:record_id:field: {ref}")

    if malformed:
        return (
            RubricCheck(
                name="evidence_refs",
                passed=False,
                detail="malformed evidence refs: " + ", ".join(malformed),
            ),
            warnings,
        )
    if not three_part:
        return (
            RubricCheck(
                name="evidence_refs",
                passed=False,
                detail="evidence refs must parse as source:record_id:field",
            ),
            warnings,
        )
    return (
        RubricCheck(
            name="evidence_refs",
            passed=True,
            detail="evidence refs parse as source:record_id:field",
        ),
        warnings,
    )
