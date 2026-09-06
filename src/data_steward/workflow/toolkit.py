from __future__ import annotations

import json
from typing import Any, Mapping

from data_steward.config import Settings
from data_steward.models import DataContract, InvestigationStep
from data_steward.repositories import Repository
from data_steward.tools import (
    MockAddressValidator,
    compare_records,
    diff_schemas,
    downstream_impact,
    load_schema_document,
    lookup_customer,
    lookup_lineage,
    validate_address,
)

READ_ONLY_TOOLS = (
    "lookup_customer",
    "compare_records",
    "validate_address",
    "load_contract",
    "diff_schemas",
    "lookup_lineage",
    "downstream_impact",
)

OPENAI_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_customer",
            "description": "Fetch CRM, Billing, Support, and golden records for one customer_id.",
            "parameters": {
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_records",
            "description": "Compare already retrieved records and list conflicting fields.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {
                        "type": "string",
                        "description": "Customer whose stored records should be compared.",
                    }
                },
                "required": ["customer_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_address",
            "description": "Mock-normalize and syntax-check an address. Not a deliverability check.",
            "parameters": {
                "type": "object",
                "properties": {
                    "address_line1": {"type": "string"},
                    "city": {"type": "string"},
                    "state": {"type": "string"},
                    "zip_code": {"type": "string"},
                    "country": {"type": "string"},
                },
                "required": [
                    "address_line1",
                    "city",
                    "state",
                    "zip_code",
                    "country",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_contract",
            "description": "Load the active customer contract, including source authority.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diff_schemas",
            "description": "Compare the active contract with the breaking schema fixture.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_lineage",
            "description": "Lookup lineage edges for a golden or schema target record id.",
            "parameters": {
                "type": "object",
                "properties": {"target_record_id": {"type": "string"}},
                "required": ["target_record_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "downstream_impact",
            "description": "List downstream assets impacted by field-level schema or data changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "required": ["fields"],
                "additionalProperties": False,
            },
        },
    },
]


class InvestigationToolkit:
    def __init__(
        self,
        repository: Repository,
        contract: DataContract,
        settings: Settings,
        address_validator: MockAddressValidator | None = None,
    ) -> None:
        self.repository = repository
        self.contract = contract
        self.settings = settings
        self.address_validator = address_validator or MockAddressValidator()

    def execute(self, name: str, arguments: Mapping[str, Any] | None) -> tuple[Any, InvestigationStep, bool]:
        step_index = 0
        arguments = dict(arguments or {})
        if name not in READ_ONLY_TOOLS:
            step = InvestigationStep(
                step=step_index,
                tool_name=name,
                arguments=arguments,
                result_summary=f"blocked unauthorized tool {name}",
                authorized=False,
            )
            return {"error": f"unauthorized tool: {name}"}, step, False
        payload = self._dispatch(name, arguments)
        step = InvestigationStep(
            step=step_index,
            tool_name=name,
            arguments=_public_args(name, arguments),
            result_summary=_summarize(name, payload),
            authorized=True,
        )
        return payload, step, True

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "lookup_customer":
            records = lookup_customer(self.repository, arguments["customer_id"])
            return [record.model_dump(mode="json") for record in records]
        if name == "compare_records":
            records = lookup_customer(self.repository, arguments["customer_id"])
            return compare_records(records).model_dump(mode="json")
        if name == "validate_address":
            return self.address_validator.validate(arguments).model_dump(mode="json")
        if name == "load_contract":
            return {
                "entity": self.contract.entity,
                "version": self.contract.version,
                "owner": self.contract.owner,
                "source_authority": {
                    field: [str(source) for source in sources]
                    for field, sources in self.contract.source_authority.items()
                },
                "governance": self.contract.governance.model_dump(mode="json"),
                "fields": {
                    name: {"type": field.type, "nullable": field.nullable, "classification": field.classification}
                    for name, field in self.contract.fields.items()
                },
            }
        if name == "diff_schemas":
            fixture = load_schema_document(self.settings.breaking_schema_path)
            changes = diff_schemas(
                {name: field.model_dump() for name, field in self.contract.fields.items()},
                fixture.get("fields", {}),
            )
            return [change.model_dump(mode="json") for change in changes]
        if name == "lookup_lineage":
            edges = lookup_lineage(self.repository, arguments["target_record_id"])
            return [edge.model_dump(mode="json") for edge in edges]
        if name == "downstream_impact":
            return downstream_impact(list(arguments.get("fields") or []))
        raise RuntimeError(f"unhandled tool {name}")


def _public_args(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    if name == "validate_address":
        return {key: "redacted" for key in arguments}
    return dict(arguments)


def _summarize(name: str, payload: Any) -> str:
    if name == "lookup_customer" and isinstance(payload, list):
        sources = [item.get("source") for item in payload]
        return f"retrieved {len(payload)} records from {sources}"
    if name == "compare_records" and isinstance(payload, dict):
        return f"conflicts={payload.get('conflicting_fields')}"
    if name == "validate_address" and isinstance(payload, dict):
        return f"valid={payload.get('valid')} errors={len(payload.get('errors') or [])}"
    if name == "diff_schemas" and isinstance(payload, list):
        return f"{len(payload)} schema changes, breaking={sum(1 for item in payload if item.get('breaking'))}"
    if name == "downstream_impact" and isinstance(payload, dict):
        return f"impacted fields={list(payload)}"
    return json.dumps(payload, default=str)[:240]
