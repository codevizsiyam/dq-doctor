from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from data_steward.models import (
    Approval,
    AuditEvent,
    Decision,
    Evidence,
    FieldChange,
    Incident,
    LineageEdge,
    Proposal,
    Severity,
    SourceRecord,
    SourceSystem,
)
from data_steward.sqlite import SQLiteRepository

BASE_TIME = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
STREETS = (
    "10 Main St",
    "22 Oak Avenue",
    "35 Pine Road",
    "48 Cedar Lane",
    "59 Lake Drive",
    "63 Hill Street",
)
CITIES = (("Austin", "TX"), ("Boston", "MA"), ("Seattle", "WA"))


def seed_repository(repository: SQLiteRepository) -> None:
    """Reset and load 18 deterministic address scenarios plus governance history."""
    repository.initialize()
    repository.reset()

    for index in range(1, 19):
        customer_id = f"C{index:03d}"
        city, state = CITIES[(index - 1) % len(CITIES)]
        canonical = {
            "customer_id": customer_id,
            "full_name": f"Customer {index:02d}",
            "email": f"customer{index:02d}@example.com",
            "address_line1": STREETS[(index - 1) % len(STREETS)],
            "city": city,
            "state": state,
            "zip_code": f"{73300 + index:05d}",
            "country": "US",
        }
        records = {
            SourceSystem.BILLING: canonical.copy(),
            SourceSystem.CRM: canonical.copy(),
            SourceSystem.SUPPORT: canonical.copy(),
            SourceSystem.GOLDEN: canonical.copy(),
        }
        category = (index - 1) % 6
        affected = ["address_line1"]
        if category == 0:
            records[SourceSystem.CRM]["address_line1"] = canonical["address_line1"].replace(
                " St", " Street"
            )
        elif category == 1:
            records[SourceSystem.SUPPORT]["city"] = city.upper()
            affected = ["city"]
        elif category == 2:
            records[SourceSystem.CRM]["zip_code"] = canonical["zip_code"][:4]
            affected = ["zip_code"]
        elif category == 3:
            records[SourceSystem.SUPPORT]["state"] = ""
            affected = ["state"]
        elif category == 4:
            records[SourceSystem.CRM]["country"] = "USA"
            affected = ["country"]
        else:
            records[SourceSystem.GOLDEN]["address_line1"] = f"OLD {canonical['address_line1']}"

        if index == 18:
            records[SourceSystem.CRM]["address_line1"] = "100 Harbor Blvd"
            records[SourceSystem.BILLING]["address_line1"] = "200 Market St"
            records[SourceSystem.SUPPORT]["address_line1"] = "300 Mission St"
            records[SourceSystem.GOLDEN]["address_line1"] = "100 Harbor Blvd"
            affected = ["address_line1"]

        for source, data in records.items():
            record_id = customer_id if source == SourceSystem.GOLDEN else f"{source}-{customer_id}"
            repository.save_record(
                SourceRecord(
                    record_id=record_id,
                    customer_id=customer_id,
                    source=source,
                    data=data,
                    observed_at=BASE_TIME + timedelta(minutes=index),
                )
            )

        incident = Incident(
            incident_id=f"INC-ADDR-{index:03d}",
            customer_id=customer_id,
            incident_type="address_discrepancy",
            affected_fields=affected,
            source_systems=[
                SourceSystem.CRM,
                SourceSystem.BILLING,
                SourceSystem.SUPPORT,
                SourceSystem.GOLDEN,
            ],
            violations=[
                "conflicting valid addresses with no unique canonical value"
                if index == 18
                else f"cross-system conflict in {affected[0]}"
            ],
            severity=(
                Severity.CRITICAL
                if index == 18
                else Severity.HIGH if category in (2, 3, 5) else Severity.MEDIUM
            ),
            contract_version="1.0",
            created_at=BASE_TIME + timedelta(hours=1, minutes=index),
        )
        repository.save_incident(incident)
        for source in (SourceSystem.CRM, SourceSystem.BILLING, SourceSystem.SUPPORT):
            repository.save_lineage(
                LineageEdge(
                    lineage_id=f"LIN-{index:03d}-{source}",
                    source_system=source,
                    source_record_id=f"{source}-{customer_id}",
                    target_record_id=customer_id,
                    fields=list(canonical),
                    recorded_at=BASE_TIME,
                )
            )

    for suffix, violation in (
        ("TYPE", "customer_id type changed from string to integer"),
        ("RENAME", "zip_code removed and postal_code added (probable rename)"),
    ):
        repository.save_incident(
            Incident(
                incident_id=f"INC-SCHEMA-{suffix}",
                incident_type="schema_drift",
                affected_fields=["customer_id"] if suffix == "TYPE" else ["zip_code"],
                source_systems=[SourceSystem.CRM],
                violations=[violation],
                severity=Severity.CRITICAL,
                contract_version="1.0",
                created_at=BASE_TIME + timedelta(hours=2),
            )
        )

    for downstream_id, source_system, fields in (
        ("LIN-SCHEMA-BILLING", SourceSystem.CRM, ["customer_id", "zip_code"]),
        ("LIN-SCHEMA-SUPPORT", SourceSystem.SUPPORT, ["customer_id"]),
    ):
        repository.save_lineage(
            LineageEdge(
                lineage_id=downstream_id,
                source_system=source_system,
                source_record_id=f"{source_system}-schema",
                target_record_id="SCHEMA-CUSTOMER",
                fields=fields,
                recorded_at=BASE_TIME,
            )
        )

    proposal = Proposal(
        proposal_id="PROP-SEED-001",
        incident_id="INC-ADDR-006",
        target_record_id="C006",
        changes=[
            FieldChange(
                field="address_line1",
                old_value=f"OLD {STREETS[5]}",
                new_value=STREETS[5],
            )
        ],
        confidence=0.99,
        rationale="Billing is authoritative for address fields.",
        created_at=BASE_TIME + timedelta(hours=3),
    )
    repository.save_proposal(proposal)
    repository.save_approval(
        Approval(
            approval_id="APR-SEED-001",
            proposal_id=proposal.proposal_id,
            decision=Decision.APPROVED,
            reviewer="seed-steward",
            comment="Known deterministic fixture",
            decided_at=BASE_TIME + timedelta(hours=4),
        )
    )
    repository.append_audit(
        AuditEvent(
            event_id="AUD-SEED-001",
            incident_id=proposal.incident_id,
            proposal_id=proposal.proposal_id,
            action="approved",
            actor="seed-steward",
            details={"fixture": True},
            created_at=BASE_TIME + timedelta(hours=4),
        )
    )


def reset_database(path: str | Path) -> SQLiteRepository:
    repository = SQLiteRepository(path)
    seed_repository(repository)
    return repository


def main() -> None:
    from data_steward.config import get_settings

    settings = get_settings()
    reset_database(settings.database_path)
    print(f"Seeded {settings.database_path}")


def seeded_evidence() -> Evidence:
    return Evidence(
        evidence_id="EVD-SEED-001",
        incident_id="INC-ADDR-006",
        source=SourceSystem.BILLING,
        record_id="billing-C006",
        field="address_line1",
        value=STREETS[5],
        observed_at=BASE_TIME,
        rationale="Billing is contract-authoritative for addresses.",
    )


if __name__ == "__main__":
    main()
