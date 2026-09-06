from __future__ import annotations

import re
from typing import Any, Iterable

from data_steward.models import Incident, SourceRecord
from data_steward.tools import compare_records

PRODUCT_NAME = "DQ Doctor"
PRODUCT_TAGLINE = "Diagnose first. Never operate without consent."

PINNED_INCIDENT_IDS = ("INC-ADDR-012", "INC-ADDR-018", "INC-SCHEMA-TYPE")

STATUS_GROUPS: dict[str, set[str] | None] = {
    "Needs action": {"open", "investigating", "awaiting_approval", "escalated"},
    "Awaiting": {"awaiting_approval"},
    "Escalated": {"escalated"},
    "Done": {"resolved", "rejected", "acknowledged"},
    "All": None,
}

TYPE_GROUPS: dict[str, set[str] | None] = {
    "All": None,
    "Address": {"address_discrepancy"},
    "Schema": {"schema_drift"},
}

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_FIELD_ORDER = (
    "customer_id",
    "full_name",
    "email",
    "address_line1",
    "city",
    "state",
    "zip_code",
    "country",
)
_SOURCES = ("crm", "billing", "support", "golden")


def pretty_label(value: str | None) -> str:
    return str(value or "").replace("_", " ")


def type_label(incident_type: str) -> str:
    return "Schema" if incident_type == "schema_drift" else "Address"


def is_acknowledge_only(incident_type: str, outcome: str | None = None) -> bool:
    """Schema tickets never expose Approve / Edit / Reject, even if the model mislabels them."""
    return incident_type == "schema_drift" or outcome == "report_only"


OUTCOME_HEADLINES = {
    "recommend": "Update the golden record",
    "escalate": "Escalate — do not write",
    "report_only": "Report only — no migration",
}


def outcome_headline(outcome: str | None) -> str:
    return OUTCOME_HEADLINES.get(str(outcome or ""), pretty_label(outcome))


def parse_evidence_ref(ref: str) -> dict[str, str]:
    parts = str(ref).split(":")
    if len(parts) >= 3:
        return {"source": parts[0], "record": parts[1], "field": ":".join(parts[2:])}
    if len(parts) == 2:
        return {"source": parts[0], "record": parts[1], "field": ""}
    return {"source": str(ref), "record": "", "field": ""}


def evidence_rows(refs: list[str]) -> list[dict[str, str]]:
    return [parse_evidence_ref(ref) for ref in refs]


def recommendation_change_frame(changes: list[Any]):
    import pandas as pd

    frame = pd.DataFrame(
        [
            {
                "Field": change.field if hasattr(change, "field") else change.get("field"),
                "From": _display(
                    change.old_value if hasattr(change, "old_value") else change.get("old_value")
                ),
                "To": _display(
                    change.new_value if hasattr(change, "new_value") else change.get("new_value")
                ),
            }
            for change in changes
        ]
    )

    def apply_styles(data: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=data.index, columns=data.columns)
        styles["From"] = "background-color: #FEF3C7"
        styles["To"] = "background-color: #CCFBF1; font-weight: 600"
        return styles

    return frame.style.apply(apply_styles, axis=None)


def policy_summary(governance: dict[str, Any] | None) -> tuple[bool, str]:
    if not governance:
        return False, "Policy has not run yet."
    reasons = [str(reason) for reason in (governance.get("reasons") or []) if reason]
    if governance.get("approval_required") and reasons:
        return True, "Human approval required: " + "; ".join(reasons)
    return False, "Policy: no extra approval reasons. A human still decides."


_EVIDENCE_PAREN = re.compile(r"\s*\([^)]*evidence:[^)]*\)", re.IGNORECASE)
_SKIP_SENTENCE = re.compile(
    r"^(i recommend|given the clear|note:\s*governance|obtain required)",
    re.IGNORECASE,
)


def diagnosis_facts(recommendation: Any) -> list[str]:
    """Short, structured findings — not the model paragraph."""
    facts: list[str] = []
    changes = list(getattr(recommendation, "changes", None) or [])
    for change in changes:
        field = getattr(change, "field", None) or ""
        old = _display(getattr(change, "old_value", None))
        new = _display(getattr(change, "new_value", None))
        if field:
            facts.append(f"Golden `{field}` is **{old or 'empty'}**.")
            facts.append(f"Proposed `{field}` is **{new or 'empty'}**.")
    refs = evidence_rows(list(getattr(recommendation, "evidence_refs", None) or []))
    sources = []
    for row in refs:
        source = row["source"]
        if source and source not in sources and source != "golden":
            sources.append(source)
    if sources:
        facts.append("Sources in agreement: " + ", ".join(sources) + ".")
    if getattr(recommendation, "outcome", None) == "escalate":
        facts.append("Authoritative sources disagree. Do not write a golden value.")
    if getattr(recommendation, "outcome", None) == "report_only":
        facts.append("This is a schema report. No golden-record write.")
    return facts


def diagnosis_notes(rationale: str, *, limit: int = 3) -> list[str]:
    """Leftover model sentences after stripping evidence dumps and duplicate asks."""
    text = _EVIDENCE_PAREN.sub("", rationale or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    notes: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        sentence = sentence.strip()
        if len(sentence) < 24 or _SKIP_SENTENCE.search(sentence):
            continue
        notes.append(sentence)
        if len(notes) >= limit:
            break
    return notes


def badge_color(kind: str, value: str) -> str:
    status_colors = {
        "open": "blue",
        "investigating": "violet",
        "awaiting_approval": "orange",
        "escalated": "red",
        "resolved": "green",
        "rejected": "gray",
        "acknowledged": "green",
    }
    severity_colors = {
        "low": "gray",
        "medium": "blue",
        "high": "orange",
        "critical": "red",
    }
    outcome_colors = {
        "recommend": "green",
        "escalate": "orange",
        "report_only": "blue",
    }
    if kind == "status":
        return status_colors.get(value, "gray")
    if kind == "severity":
        return severity_colors.get(value, "gray")
    if kind == "outcome":
        return outcome_colors.get(value, "gray")
    return "gray"


def filter_incidents(
    incidents: Iterable[Incident],
    *,
    status_group: str = "Needs action",
    type_group: str = "All",
) -> list[Incident]:
    statuses = STATUS_GROUPS.get(status_group, STATUS_GROUPS["Needs action"])
    types = TYPE_GROUPS.get(type_group, None)
    result: list[Incident] = []
    for incident in incidents:
        if statuses is not None and str(incident.status) not in statuses:
            continue
        if types is not None and incident.incident_type not in types:
            continue
        result.append(incident)
    return result


def order_incidents(incidents: Iterable[Incident]) -> tuple[list[Incident], list[Incident]]:
    items = list(incidents)
    by_id = {incident.incident_id: incident for incident in items}
    pinned = [by_id[incident_id] for incident_id in PINNED_INCIDENT_IDS if incident_id in by_id]
    pinned_ids = {incident.incident_id for incident in pinned}
    rest = [incident for incident in items if incident.incident_id not in pinned_ids]
    rest.sort(
        key=lambda incident: (
            _SEVERITY_RANK.get(str(incident.severity), 9),
            incident.incident_id,
        )
    )
    return pinned, rest


def comparison_rows(
    records: list[SourceRecord],
    proposed: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not records:
        return []
    comparison = compare_records(records)
    fields = list(_FIELD_ORDER)
    for field in comparison.values_by_field:
        if field not in fields:
            fields.append(field)
    proposed = proposed or {}
    rows: list[dict[str, Any]] = []
    for field in fields:
        if field not in comparison.values_by_field:
            continue
        values = comparison.values_by_field[field]
        row: dict[str, Any] = {"field": field, "conflict": field in comparison.conflicting_fields}
        for source in _SOURCES:
            row[source] = values.get(source)
        if field in proposed:
            row["proposed"] = proposed[field]
        rows.append(row)
    return rows


def comparison_frame(rows: list[dict[str, Any]]):
    """Native table for Streamlit — stays inside layout, unlike raw HTML."""
    import pandas as pd

    frame = pd.DataFrame(
        [
            {
                "Field": row["field"],
                "CRM": _display(row.get("crm")),
                "Billing": _display(row.get("billing")),
                "Support": _display(row.get("support")),
                "Golden": _display(row.get("golden")),
                "Proposed": _display(row.get("proposed")),
            }
            for row in rows
        ]
    )
    conflicts = [bool(row.get("conflict")) for row in rows]
    proposed_flags = [row.get("proposed") not in (None, "") for row in rows]

    def apply_styles(data: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=data.index, columns=data.columns)
        for index, conflict in enumerate(conflicts):
            if conflict:
                for column in ("CRM", "Billing", "Support", "Golden"):
                    styles.iat[index, styles.columns.get_loc(column)] = "background-color: #FEF3C7"
            if proposed_flags[index]:
                styles.iat[index, styles.columns.get_loc("Proposed")] = (
                    "background-color: #CCFBF1; font-weight: 600"
                )
        return styles

    return frame.style.apply(apply_styles, axis=None)


def schema_diff_rows(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for change in changes:
        rows.append(
            {
                "field": change.get("field"),
                "kind": pretty_label(change.get("kind")),
                "old": _display(change.get("old_value")),
                "new": _display(change.get("new_value")),
                "breaking": "yes" if change.get("breaking") else "no",
            }
        )
    return rows


def downstream_chips(downstream: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
    return [(field, list(assets)) for field, assets in downstream.items()]


def changed_field_pairs(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[dict[str, Any]]:
    before = before or {}
    after = after or {}
    fields = sorted(set(before) | set(after))
    rows: list[dict[str, Any]] = []
    for field in fields:
        if before.get(field) == after.get(field):
            continue
        rows.append({"field": field, "before": _display(before.get(field)), "after": _display(after.get(field))})
    return rows


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return str(value)
    return str(value)
