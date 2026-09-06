from pathlib import Path

import yaml

from data_steward.governance import assess_governance, validate_proposal
from data_steward.models import FieldChange, Proposal
from data_steward.seed import seed_repository
from data_steward.sqlite import SQLiteRepository
from data_steward.tools import diff_schemas, load_contract

ROOT = Path(__file__).parents[1]


def test_breaking_schema_fixture_detects_type_change_and_rename():
    contract = load_contract(ROOT / "contracts/customer.yaml")
    fixture = yaml.safe_load(
        (ROOT / "fixtures/customer_breaking_schema.yaml").read_text(encoding="utf-8")
    )
    changes = diff_schemas(
        {name: field.model_dump() for name, field in contract.fields.items()},
        fixture["fields"],
    )

    assert any(
        change.kind == "type_changed" and change.field == "customer_id"
        for change in changes
    )
    assert any(
        change.kind == "renamed"
        and change.field == "zip_code"
        and change.new_value == "postal_code"
        for change in changes
    )
    assert all(change.breaking for change in changes)


def test_proposal_validation_and_policy_require_approval(tmp_path):
    repository = SQLiteRepository(tmp_path / "steward.db")
    seed_repository(repository)
    contract = load_contract(ROOT / "contracts/customer.yaml")
    target = repository.get_record("golden", "C001")
    proposal = Proposal(
        proposal_id="PROP-TEST",
        incident_id="INC-ADDR-001",
        target_record_id="C001",
        changes=[
            FieldChange(
                field="address_line1",
                old_value=target.data["address_line1"],
                new_value="11 MAIN ST",
            )
        ],
        confidence=0.99,
        rationale="Test",
    )

    assert validate_proposal(proposal, target, contract).valid
    assessment = assess_governance(proposal, contract, 0.85)
    assert assessment.approval_required
    assert "PII change" in assessment.reasons
    assert "mandatory approval field" in assessment.reasons
