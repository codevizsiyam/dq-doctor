from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Protocol

import yaml

from data_steward.models import (
    AddressValidation,
    DataContract,
    RecordComparison,
    SchemaChange,
    SourceRecord,
)
from data_steward.repositories import Repository

DOWNSTREAM_SYSTEMS = {
    "customer_id": [
        "billing.invoices.customer_id",
        "support.tickets.customer_id",
        "analytics.customer_360.customer_id",
    ],
    "zip_code": [
        "billing.tax_jurisdiction.zip_code",
        "logistics.routing.zip_code",
        "analytics.customer_360.zip_code",
    ],
    "postal_code": [
        "billing.tax_jurisdiction.zip_code",
        "logistics.routing.zip_code",
        "analytics.customer_360.zip_code",
    ],
    "address_line1": ["logistics.shipping.address_line1", "analytics.customer_360.address"],
    "email": ["support.tickets.email", "marketing.opt_in.email"],
}

ZIP_PATTERN = re.compile(r"^\d{5}(?:-\d{4})?$")
STATE_PATTERN = re.compile(r"^[A-Z]{2}$")
STREET_REPLACEMENTS = {
    " STREET": " ST",
    " AVENUE": " AVE",
    " ROAD": " RD",
    " LANE": " LN",
    " DRIVE": " DR",
}


class AddressValidator(Protocol):
    def validate(self, address: Mapping[str, Any]) -> AddressValidation: ...


class MockAddressValidator:
    """Demo-stable validator. Swap this class for a Google Address Validation adapter later."""

    def validate(self, address: Mapping[str, Any]) -> AddressValidation:
        return validate_address(address)


def load_contract(path: str | Path) -> DataContract:
    with Path(path).open(encoding="utf-8") as handle:
        return DataContract.model_validate(yaml.safe_load(handle))


def load_schema_document(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError("schema document must be a mapping")
    return loaded


def lookup_customer(repository: Repository, customer_id: str) -> list[SourceRecord]:
    """Read-only stable lookup ordered by repository source and record identifier."""
    return repository.find_records(customer_id)


def compare_records(records: list[SourceRecord]) -> RecordComparison:
    if not records:
        raise ValueError("at least one record is required")
    customer_ids = {record.customer_id for record in records}
    if len(customer_ids) != 1:
        raise ValueError("records must belong to one customer")
    fields = sorted({field for record in records for field in record.data})
    values = {
        field: {str(record.source): record.data.get(field) for record in records}
        for field in fields
    }
    conflicts = [
        field
        for field, by_source in values.items()
        if len({_stable_value(value) for value in by_source.values()}) > 1
    ]
    return RecordComparison(
        customer_id=records[0].customer_id,
        values_by_field=values,
        conflicting_fields=conflicts,
    )


def validate_address(address: Mapping[str, Any]) -> AddressValidation:
    """Deterministic mock validator: syntax and normalization only, no deliverability claim."""
    normalized = {
        field: str(address.get(field, "")).strip().upper()
        for field in ("address_line1", "city", "state", "zip_code", "country")
    }
    street = normalized["address_line1"]
    for long_name, abbreviation in STREET_REPLACEMENTS.items():
        if street.endswith(long_name):
            street = street[: -len(long_name)] + abbreviation
            break
    normalized["address_line1"] = " ".join(street.split())
    errors: list[str] = []
    for field in ("address_line1", "city", "state", "zip_code", "country"):
        if not normalized[field]:
            errors.append(f"{field} is required")
    if normalized["state"] and not STATE_PATTERN.fullmatch(normalized["state"]):
        errors.append("state must be a two-letter code")
    if normalized["zip_code"] and not ZIP_PATTERN.fullmatch(normalized["zip_code"]):
        errors.append("zip_code must be five digits or ZIP+4")
    if normalized["country"] not in {"US", "CA", "GB"}:
        errors.append("country is not allowed")
    return AddressValidation(valid=not errors, normalized=normalized, errors=errors)


def diff_schemas(
    expected_fields: Mapping[str, Mapping[str, Any]],
    actual_fields: Mapping[str, Mapping[str, Any]],
) -> list[SchemaChange]:
    changes: list[SchemaChange] = []
    removed = set(expected_fields) - set(actual_fields)
    added = set(actual_fields) - set(expected_fields)
    if "zip_code" in removed and "postal_code" in added:
        changes.append(
            SchemaChange(
                kind="renamed",
                field="zip_code",
                old_value="zip_code",
                new_value="postal_code",
                breaking=True,
            )
        )
        removed.remove("zip_code")
        added.remove("postal_code")
    for field in sorted(removed):
        changes.append(SchemaChange(kind="removed", field=field, breaking=True))
    for field in sorted(added):
        classification = actual_fields[field].get("classification")
        changes.append(
            SchemaChange(
                kind="added",
                field=field,
                new_value=actual_fields[field],
                breaking=classification == "pii",
            )
        )
    for field in sorted(set(expected_fields) & set(actual_fields)):
        expected, actual = expected_fields[field], actual_fields[field]
        for attribute in ("type", "nullable"):
            if expected.get(attribute) != actual.get(attribute):
                changes.append(
                    SchemaChange(
                        kind=f"{attribute}_changed",
                        field=field,
                        old_value=expected.get(attribute),
                        new_value=actual.get(attribute),
                        breaking=True,
                    )
                )
    return changes


def lookup_lineage(repository: Repository, target_record_id: str):
    return repository.get_lineage(target_record_id)


def downstream_impact(fields: list[str]) -> dict[str, list[str]]:
    """Read-only metadata: which downstream assets consume the supplied fields."""
    return {
        field: list(DOWNSTREAM_SYSTEMS.get(field, ["analytics.customer_360"]))
        for field in fields
    }


def _stable_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, sort_keys=True)
    return repr(value)
