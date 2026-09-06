from __future__ import annotations

from typing import Any

from data_steward.models import FieldChange, Incident, Recommendation, SourceSystem
from data_steward.tools import compare_records, lookup_customer
from data_steward.workflow.toolkit import InvestigationToolkit

COUNTRY_ALIASES = {
    "USA": "US",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "CANADA": "CA",
    "UK": "GB",
    "UNITED KINGDOM": "GB",
    "GREAT BRITAIN": "GB",
}


def fallback_recommendation(
    incident: Incident,
    toolkit: InvestigationToolkit,
) -> tuple[Recommendation, list[dict[str, Any]], dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    extras: dict[str, Any] = {"schema_changes": [], "downstream": {}}
    if incident.incident_type == "schema_drift":
        recommendation = _schema_fallback(incident, toolkit, steps, extras)
    else:
        recommendation = _address_fallback(incident, toolkit, steps)
    recommendation.fallback_used = True
    recommendation.tools_used = list(dict.fromkeys(step["tool_name"] for step in steps))
    return recommendation, steps, extras


def _recorded(toolkit: InvestigationToolkit, steps: list[dict[str, Any]], name: str, arguments: dict[str, Any]):
    payload, step, _authorized = toolkit.execute(name, arguments)
    recorded = step.model_dump(mode="json")
    recorded["step"] = len(steps) + 1
    steps.append(recorded)
    return payload


def _schema_fallback(
    incident: Incident,
    toolkit: InvestigationToolkit,
    steps: list[dict[str, Any]],
    extras: dict[str, Any],
) -> Recommendation:
    _recorded(toolkit, steps, "load_contract", {})
    changes = _recorded(toolkit, steps, "diff_schemas", {})
    fields = list(incident.affected_fields)
    extras["schema_changes"] = changes
    extras["downstream"] = _recorded(toolkit, steps, "downstream_impact", {"fields": fields})
    _recorded(toolkit, steps, "lookup_lineage", {"target_record_id": "SCHEMA-CUSTOMER"})
    breaking = [item for item in changes if item.get("breaking")]
    return Recommendation(
        outcome="report_only",
        incident_id=incident.incident_id,
        classified_type="schema_drift",
        confidence=0.99,
        uncertainty="A human must decide whether to version the contract; the agent will not migrate schemas.",
        impact="Breaking schema change would affect downstream billing, support, and analytics assets.",
        rationale=(
            "Detected breaking schema drift against customer contract v"
            f"{toolkit.contract.version}. Changes: {breaking}. "
            "Policy forbids automatic migration."
        ),
        evidence_refs=[
            f"contract:{toolkit.contract.version}",
            "fixture:customer_breaking_schema",
            "lineage:SCHEMA-CUSTOMER",
        ],
        candidate_value={"migration": "none"},
        tools_used=[],
        fallback_used=True,
    )


def _address_fallback(
    incident: Incident,
    toolkit: InvestigationToolkit,
    steps: list[dict[str, Any]],
) -> Recommendation:
    customer_id = incident.customer_id or ""
    raw_records = _recorded(toolkit, steps, "lookup_customer", {"customer_id": customer_id})
    comparison = _recorded(toolkit, steps, "compare_records", {"customer_id": customer_id})
    contract_view = _recorded(toolkit, steps, "load_contract", {})
    records = lookup_customer(toolkit.repository, customer_id)
    by_source = {str(record.source): record for record in records}
    golden = by_source.get(SourceSystem.GOLDEN)
    conflicting = list(comparison.get("conflicting_fields") or incident.affected_fields)
    validations = {}
    for record in records:
        payload = _recorded(toolkit, steps, "validate_address", record.data)
        validations[str(record.source)] = payload

    distinct_valid_streets = {
        validations[source]["normalized"]["address_line1"]
        for source, payload in validations.items()
        if payload.get("valid") and source != SourceSystem.GOLDEN
    }
    if len(distinct_valid_streets) >= 3:
        return Recommendation(
            outcome="escalate",
            incident_id=incident.incident_id,
            classified_type="address_discrepancy",
            target_record_id=golden.record_id if golden else "",
            confidence=0.34,
            uncertainty="CRM, Billing, and Support each have a different valid street. Source authority cannot break the tie.",
            impact="Writing any one address would silently discard two other plausible customer locations.",
            rationale=(
                "Escalating because three source systems disagree on address_line1 with equally valid values. "
                f"Observed streets: {sorted(distinct_valid_streets)}."
            ),
            evidence_refs=[
                f"{source}:{by_source[source].record_id}:address_line1"
                for source in ("crm", "billing", "support")
                if source in by_source
            ],
            candidate_value={},
            fallback_used=True,
        )

    candidate: dict[str, Any] = {}
    changes: list[FieldChange] = []
    evidence_refs: list[str] = []
    authority = contract_view.get("source_authority") or {}
    for field in conflicting:
        chosen_source, chosen_value = _choose_value(field, by_source, validations, authority)
        if chosen_source is None:
            continue
        candidate[field] = chosen_value
        evidence_refs.append(f"{chosen_source}:{by_source[chosen_source].record_id}:{field}")
        if golden and golden.data.get(field) != chosen_value:
            changes.append(
                FieldChange(field=field, old_value=golden.data.get(field), new_value=chosen_value)
            )

    if not candidate and golden is not None:
        field = (incident.affected_fields or ["address_line1"])[0]
        candidate[field] = golden.data.get(field)
        evidence_refs.append(f"golden:{golden.record_id}:{field}")

    return Recommendation(
        outcome="recommend",
        incident_id=incident.incident_id,
        classified_type="address_discrepancy",
        target_record_id=golden.record_id if golden else "",
        changes=changes,
        candidate_value=candidate,
        evidence_refs=evidence_refs,
        confidence=0.93 if changes else 0.88,
        uncertainty="Formatting-equivalent values were collapsed using the mock validator and billing authority.",
        impact="Updating the golden customer address used by downstream billing and logistics.",
        rationale=_address_rationale(conflicting, candidate, changes, compare_records(records)),
        fallback_used=True,
    )


def _choose_value(
    field: str,
    by_source: dict[str, Any],
    validations: dict[str, Any],
    authority: dict[str, list[str]],
) -> tuple[str | None, Any]:
    ranked = [str(source) for source in authority.get(field, ["billing", "crm", "support"])]
    ranked = [source for source in ranked if source in by_source]
    for source in ranked:
        original = by_source[source].data.get(field)
        payload = validations.get(source) or {}
        if field == "country":
            alias = COUNTRY_ALIASES.get(str(original or "").upper())
            if alias:
                return source, alias
            if payload.get("valid"):
                return source, payload["normalized"]["country"]
            continue
        if field in {"address_line1", "city", "state", "zip_code"}:
            if payload.get("valid"):
                if field == "city":
                    return source, str(original).strip().title()
                if field in {"state", "zip_code"}:
                    return source, payload["normalized"][field]
                return source, " ".join(str(original).split())
            continue
        if original not in (None, ""):
            return source, original
    for source in ranked:
        value = by_source[source].data.get(field)
        if value not in (None, ""):
            return source, value
    return None, None


def _address_rationale(
    conflicting: list[str],
    candidate: dict[str, Any],
    changes: list[FieldChange],
    comparison: Any,
) -> str:
    if changes:
        changed = ", ".join(f"{change.field} -> {change.new_value!r}" for change in changes)
        return (
            f"Billing is the contract-authoritative source for address fields. "
            f"Conflicts in {conflicting} resolve to {candidate}. Proposed golden updates: {changed}."
        )
    return (
        f"Source conflicts in {conflicting} are equivalent after normalization. "
        f"Golden already holds the canonical values {candidate}."
    )
