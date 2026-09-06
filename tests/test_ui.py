from data_steward.models import FieldChange, Incident, Severity, SourceRecord, SourceSystem
from data_steward.ui import (
    comparison_frame,
    comparison_rows,
    diagnosis_facts,
    diagnosis_notes,
    evidence_rows,
    filter_incidents,
    is_acknowledge_only,
    order_incidents,
    outcome_headline,
    parse_evidence_ref,
    policy_summary,
    pretty_label,
    recommendation_change_frame,
    schema_diff_rows,
    type_label,
)


def _incident(incident_id: str, incident_type: str = "address_discrepancy", status: str = "open", severity: str = "medium") -> Incident:
    return Incident(
        incident_id=incident_id,
        customer_id="C001",
        incident_type=incident_type,
        affected_fields=["address_line1"],
        source_systems=[SourceSystem.CRM],
        violations=["conflict"],
        severity=Severity(severity),
        contract_version="1.0",
        status=status,
    )


def test_order_incidents_pins_demo_cases_first():
    incidents = [
        _incident("INC-ADDR-003"),
        _incident("INC-SCHEMA-TYPE", "schema_drift", severity="critical"),
        _incident("INC-ADDR-012"),
        _incident("INC-ADDR-018", severity="critical"),
    ]
    pinned, rest = order_incidents(incidents)
    assert [item.incident_id for item in pinned] == [
        "INC-ADDR-012",
        "INC-ADDR-018",
        "INC-SCHEMA-TYPE",
    ]
    assert [item.incident_id for item in rest] == ["INC-ADDR-003"]


def test_filter_incidents_needs_action_and_schema():
    incidents = [
        _incident("INC-ADDR-012", status="open"),
        _incident("INC-ADDR-001", status="resolved"),
        _incident("INC-SCHEMA-TYPE", "schema_drift", status="open"),
    ]
    needs = filter_incidents(incidents, status_group="Needs action")
    assert {item.incident_id for item in needs} == {"INC-ADDR-012", "INC-SCHEMA-TYPE"}
    schema = filter_incidents(incidents, status_group="All", type_group="Schema")
    assert [item.incident_id for item in schema] == ["INC-SCHEMA-TYPE"]


def test_comparison_rows_mark_conflicts_and_proposed():
    records = [
        SourceRecord(
            record_id="crm-C001",
            customer_id="C001",
            source=SourceSystem.CRM,
            data={"address_line1": "10 Main Street", "city": "Austin"},
            observed_at="2026-01-01T12:00:00Z",
        ),
        SourceRecord(
            record_id="C001",
            customer_id="C001",
            source=SourceSystem.GOLDEN,
            data={"address_line1": "10 Main St", "city": "Austin"},
            observed_at="2026-01-01T12:00:00Z",
        ),
    ]
    rows = {row["field"]: row for row in comparison_rows(records, {"address_line1": "10 Main St"})}
    assert rows["address_line1"]["conflict"] is True
    assert rows["address_line1"]["proposed"] == "10 Main St"
    assert rows["city"]["conflict"] is False
    frame = comparison_frame(list(rows.values()))
    assert list(frame.data.columns) == ["Field", "CRM", "Billing", "Support", "Golden", "Proposed"]


def test_schema_diff_rows_and_labels():
    rows = schema_diff_rows(
        [{"field": "zip_code", "kind": "renamed", "old_value": "zip_code", "new_value": "postal_code", "breaking": True}]
    )
    assert rows[0]["breaking"] == "yes"
    assert rows[0]["kind"] == "renamed"
    assert type_label("schema_drift") == "Schema"
    assert pretty_label("awaiting_approval") == "awaiting approval"
    assert is_acknowledge_only("schema_drift", "recommend") is True
    assert is_acknowledge_only("address_discrepancy", "recommend") is False
    assert is_acknowledge_only("address_discrepancy", "report_only") is True


def test_recommendation_display_helpers():
    assert outcome_headline("recommend") == "Update the golden record"
    assert outcome_headline("escalate") == "Escalate — do not write"
    assert outcome_headline("report_only") == "Report only — no migration"
    parsed = parse_evidence_ref("billing:billing-C012:address_line1")
    assert parsed == {"source": "billing", "record": "billing-C012", "field": "address_line1"}
    rows = evidence_rows(["crm:crm-C012:address_line1", "contract:1.0"])
    assert rows[0]["source"] == "crm"
    assert rows[1]["record"] == "1.0"
    required, line = policy_summary({"approval_required": True, "reasons": ["PII change"]})
    assert required is True
    assert "PII change" in line
    frame = recommendation_change_frame(
        [FieldChange(field="address_line1", old_value="OLD 63 Hill Street", new_value="63 Hill Street")]
    )
    assert list(frame.data.columns) == ["Field", "From", "To"]
    assert frame.data.iloc[0]["To"] == "63 Hill Street"
    blob = (
        "Three authoritative source systems (billing, crm, support) all contain "
        "'63 Hill Street' while the current golden record contains 'OLD 63 Hill Street' "
        "(evidence: billing:billing-C012:address_line1, crm:crm-C012:address_line1). "
        "I recommend updating the golden record address_line1 from 'OLD 63 Hill Street' "
        "to '63 Hill Street'. Note: governance requires approval for PII changes."
    )
    rec = type(
        "Rec",
        (),
        {
            "changes": [
                FieldChange(
                    field="address_line1",
                    old_value="OLD 63 Hill Street",
                    new_value="63 Hill Street",
                )
            ],
            "evidence_refs": [
                "billing:billing-C012:address_line1",
                "crm:crm-C012:address_line1",
                "support:support-C012:address_line1",
                "golden:C012:address_line1",
            ],
            "outcome": "recommend",
            "rationale": blob,
        },
    )()
    facts = diagnosis_facts(rec)
    assert any("OLD 63 Hill Street" in fact for fact in facts)
    assert any("63 Hill Street" in fact and "Proposed" in fact for fact in facts)
    assert any("billing" in fact for fact in facts)
    notes = diagnosis_notes(blob)
    assert all("evidence:" not in note.lower() for note in notes)
    assert all(not note.lower().startswith("i recommend") for note in notes)
