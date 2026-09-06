from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from data_steward.models import FieldChange, Incident, Recommendation
from data_steward.workflow.fallback import fallback_recommendation
from data_steward.workflow.toolkit import OPENAI_TOOL_SPECS, InvestigationToolkit

SYSTEM_PROMPT = """You are the single Data Steward Investigator Agent.
Use only the provided read-only tools. Never invent records, never write data, and never call unauthorized tools.
Investigate the incident, then stop when you have enough evidence to either:
- recommend a canonical golden-record value with evidence citations, or
- escalate because the evidence is ambiguous, or
- report a schema change and its downstream impact without migrating it.
Cite evidence_refs as source:record_id:field. Confidence alone never authorizes a write.
"""

JsonPrimitive = str | int | float | bool | None


class OpenAIFieldChange(BaseModel):
    """Strict schema for OpenAI structured outputs (every property required + typed)."""

    model_config = ConfigDict(extra="forbid")

    field: str
    old_value: JsonPrimitive
    new_value: JsonPrimitive


class OpenAICandidateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: JsonPrimitive


class OpenAIRecommendation(BaseModel):
    """OpenAI-parseable recommendation. Converted to Recommendation after the call."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["recommend", "escalate", "report_only"]
    incident_id: str
    classified_type: str
    target_record_id: str
    changes: list[OpenAIFieldChange]
    candidate_entries: list[OpenAICandidateEntry]
    evidence_refs: list[str]
    confidence: float = Field(ge=0, le=1)
    uncertainty: str
    impact: str
    rationale: str


def _to_recommendation(parsed: OpenAIRecommendation) -> Recommendation:
    return Recommendation(
        outcome=parsed.outcome,
        incident_id=parsed.incident_id,
        classified_type=parsed.classified_type,
        target_record_id=parsed.target_record_id,
        changes=[
            FieldChange(
                field=change.field,
                old_value=change.old_value,
                new_value=change.new_value,
            )
            for change in parsed.changes
        ],
        candidate_value={entry.key: entry.value for entry in parsed.candidate_entries},
        evidence_refs=parsed.evidence_refs,
        confidence=parsed.confidence,
        uncertainty=parsed.uncertainty,
        impact=parsed.impact,
        rationale=parsed.rationale,
        tools_used=[],
        fallback_used=False,
    )


def run_investigator(
    incident: Incident,
    toolkit: InvestigationToolkit,
    *,
    force_fallback: bool = False,
) -> tuple[Recommendation, list[dict[str, Any]], dict[str, Any]]:
    if force_fallback or not toolkit.settings.openai_api_key:
        return fallback_recommendation(incident, toolkit)
    try:
        return _run_openai(incident, toolkit)
    except Exception as exc:  # noqa: BLE001 - demo must survive provider outages
        recommendation, steps, extras = fallback_recommendation(incident, toolkit)
        recommendation.rationale = (
            f"OpenAI unavailable ({exc.__class__.__name__}); used deterministic fallback. "
            + recommendation.rationale
        )
        extras["openai_error"] = str(exc)
        return recommendation, steps, extras


def _run_openai(
    incident: Incident,
    toolkit: InvestigationToolkit,
) -> tuple[Recommendation, list[dict[str, Any]], dict[str, Any]]:
    from openai import OpenAI

    client = OpenAI(api_key=toolkit.settings.openai_api_key)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "incident": incident.model_dump(mode="json"),
                    "instructions": (
                        "Investigate with tools, then produce the final recommendation. "
                        "Put proposed field values in candidate_entries as key/value pairs "
                        "and mirror golden-record edits in changes."
                    ),
                },
                default=str,
            ),
        },
    ]
    steps: list[dict[str, Any]] = []
    extras: dict[str, Any] = {"schema_changes": [], "downstream": {}}
    for _ in range(toolkit.settings.max_investigator_steps):
        response = client.chat.completions.create(
            model=toolkit.settings.openai_model,
            messages=messages,
            tools=OPENAI_TOOL_SPECS,
            tool_choice="auto",
        )
        message = response.choices[0].message
        if not message.tool_calls:
            break
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in message.tool_calls
                ],
            }
        )
        for call in message.tool_calls:
            arguments = json.loads(call.function.arguments or "{}")
            payload, step, _authorized = toolkit.execute(call.function.name, arguments)
            recorded = step.model_dump(mode="json")
            recorded["step"] = len(steps) + 1
            steps.append(recorded)
            if call.function.name == "diff_schemas" and isinstance(payload, list):
                extras["schema_changes"] = payload
            if call.function.name == "downstream_impact" and isinstance(payload, dict):
                extras["downstream"] = payload
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(payload, default=str)[:8000],
                }
            )
    parsed = client.chat.completions.parse(
        model=toolkit.settings.openai_model,
        messages=messages
        + [
            {
                "role": "user",
                "content": (
                    "Return the final OpenAIRecommendation. Use only retrieved evidence. "
                    "candidate_entries must list proposed field values as key/value objects."
                ),
            }
        ],
        response_format=OpenAIRecommendation,
    )
    raw = parsed.choices[0].message.parsed
    if raw is None:
        raise RuntimeError("model did not return a structured recommendation")
    recommendation = _to_recommendation(raw)
    recommendation.incident_id = incident.incident_id
    recommendation.tools_used = list(dict.fromkeys(step["tool_name"] for step in steps))
    recommendation.fallback_used = False
    if not recommendation.classified_type:
        recommendation.classified_type = incident.incident_type
    if not steps:
        return fallback_recommendation(incident, toolkit)
    return recommendation, steps, extras
