from data_steward.models import FieldChange, Incident, Recommendation, Severity, SourceRecord, SourceSystem
from data_steward.rubric import candidate_values_grounded, grade_recommendation
from data_steward.tools import distinct_valid_operational_streets, lookup_customer, validate_address
from data_steward.seed import seed_repository
from data_steward.sqlite import SQLiteRepository
from data_steward.ui import write_actions_hidden


def _incident(**kwargs) -> Incident:
    payload = {
        "incident_id": "INC-ADDR-012",
        "customer_id": "C012",
        "incident_type": "address_discrepancy",
        "affected_fields": ["address_line1"],
        "source_systems": [SourceSystem.CRM],
        "violations": ["conflict"],
        "severity": Severity.HIGH,
        "contract_version": "1.0",
    }
    payload.update(kwargs)
    return Incident(**payload)


def _recommend(**kwargs) -> Recommendation:
    payload = {
        "outcome": "recommend",
        "incident_id": "INC-ADDR-012",
        "classified_type": "address_discrepancy",
        "target_record_id": "C012",
        "changes": [FieldChange(field="address_line1", old_value="OLD 63 Hill Street", new_value="63 Hill Street")],
        "candidate_value": {"address_line1": "63 Hill Street"},
        "evidence_refs": ["billing:billing-C012:address_line1"],
        "confidence": 0.9,
        "uncertainty": "",
        "impact": "update golden",
        "rationale": "Billing has 63 Hill Street",
    }
    payload.update(kwargs)
    return Recommendation(**payload)


def test_grounded_address_recommendation_is_satisfied(tmp_path):
    repository = SQLiteRepository(tmp_path / "steward.db")
    seed_repository(repository)
    records = lookup_customer(repository, "C012")
    result = grade_recommendation(_incident(), _recommend(), records)
    assert result.verdict == "satisfied"
    assert candidate_values_grounded(_recommend(), records)
    assert not write_actions_hidden(result.model_dump())


def test_hallucinated_street_needs_revision(tmp_path):
    repository = SQLiteRepository(tmp_path / "steward.db")
    seed_repository(repository)
    records = lookup_customer(repository, "C012")
    rec = _recommend(
        candidate_value={"address_line1": "999 Fake St"},
        changes=[FieldChange(field="address_line1", old_value="OLD 63 Hill Street", new_value="999 Fake St")],
    )
    result = grade_recommendation(_incident(), rec, records)
    assert result.verdict == "needs_revision"
    assert any(check.name == "grounded_values" and not check.passed for check in result.checks)
    assert write_actions_hidden(result.model_dump())


def test_three_valid_streets_cannot_recommend_a_write(tmp_path):
    repository = SQLiteRepository(tmp_path / "steward.db")
    seed_repository(repository)
    records = lookup_customer(repository, "C018")
    assert len(distinct_valid_operational_streets(records)) >= 3
    rec = _recommend(
        incident_id="INC-ADDR-018",
        candidate_value={"address_line1": "100 Harbor Blvd"},
        changes=[FieldChange(field="address_line1", old_value="x", new_value="100 Harbor Blvd")],
        evidence_refs=["crm:crm-C018:address_line1"],
    )
    result = grade_recommendation(_incident(incident_id="INC-ADDR-018", customer_id="C018"), rec, records)
    assert result.verdict == "needs_revision"
    assert any(check.name == "three_street_escalate" and not check.passed for check in result.checks)


def test_schema_report_fails_if_writes_remain():
    rec = _recommend(
        outcome="report_only",
        classified_type="schema_drift",
        incident_id="INC-SCHEMA-TYPE",
        candidate_value={"migration": "none"},
        changes=[],
        evidence_refs=["contract:1.0", "fixture:customer_breaking_schema"],
    )
    incident = _incident(
        incident_id="INC-SCHEMA-TYPE",
        customer_id="",
        incident_type="schema_drift",
        affected_fields=["customer_id"],
    )
    ok = grade_recommendation(incident, rec, [])
    assert ok.verdict == "satisfied"
    bad = rec.model_copy(
        update={"changes": [FieldChange(field="address_line1", old_value="a", new_value="b")]}
    )
    failed = grade_recommendation(incident, bad, [])
    assert failed.verdict == "needs_revision"


def test_malformed_evidence_refs_fail_address_writes():
    rec = _recommend(evidence_refs=["not-a-citation"])
    result = grade_recommendation(_incident(), rec, [])
    assert result.verdict == "needs_revision"
    assert any(check.name == "evidence_refs" and not check.passed for check in result.checks)


def test_validate_address_still_accepts_canonical_street():
    assert validate_address(
        {
            "address_line1": "63 Hill Street",
            "city": "Austin",
            "state": "TX",
            "zip_code": "73312",
            "country": "US",
        }
    ).valid
