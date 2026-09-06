from pathlib import Path

import pytest

from data_steward.models import FieldChange, Proposal
from data_steward.remediation import RemediationError, RemediationService
from data_steward.seed import seed_repository
from data_steward.sqlite import SQLiteRepository
from data_steward.tools import load_contract

ROOT = Path(__file__).parents[1]


def test_remediation_requires_approval_and_is_audited_and_idempotent(tmp_path):
    repository = SQLiteRepository(tmp_path / "steward.db")
    seed_repository(repository)
    service = RemediationService(
        repository, load_contract(ROOT / "contracts/customer.yaml")
    )

    result = service.apply("PROP-SEED-001")
    assert result.applied
    assert result.record.data["address_line1"] == "63 Hill Street"
    event = next(
        event
        for event in repository.get_audit("INC-ADDR-006")
        if event.action == "remediation_applied"
    )
    assert event.before["address_line1"] == "OLD 63 Hill Street"
    assert event.after["address_line1"] == "63 Hill Street"
    assert event.verification_succeeded
    assert repository.get_incident("INC-ADDR-006").status == "resolved"

    replay = service.apply("PROP-SEED-001")
    assert not replay.applied
    assert replay.idempotent_replay


def test_unapproved_and_stale_proposals_cannot_write(tmp_path):
    repository = SQLiteRepository(tmp_path / "steward.db")
    seed_repository(repository)
    proposal = Proposal(
        proposal_id="PROP-NO-APPROVAL",
        incident_id="INC-ADDR-001",
        target_record_id="C001",
        changes=[
            FieldChange(
                field="address_line1",
                old_value="not the stored value",
                new_value="11 MAIN ST",
            )
        ],
        confidence=1,
        rationale="Unsafe test",
    )
    repository.save_proposal(proposal)
    service = RemediationService(
        repository, load_contract(ROOT / "contracts/customer.yaml")
    )
    before = repository.get_record("golden", "C001")

    with pytest.raises(RemediationError, match="approved decision"):
        service.apply(proposal.proposal_id)

    assert repository.get_record("golden", "C001") == before
