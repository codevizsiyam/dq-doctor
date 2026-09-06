from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st

st.set_page_config(
    page_title="DQ Doctor",
    page_icon=":material/verified_user:",
    layout="wide",
    initial_sidebar_state="expanded",
)

from data_steward.config import get_settings
from data_steward.models import FieldChange, HumanDecision
from data_steward.observability.phoenix import configure_phoenix
from data_steward.ui import (
    PINNED_INCIDENT_IDS,
    STATUS_GROUPS,
    TYPE_GROUPS,
    badge_color,
    changed_field_pairs,
    comparison_frame,
    comparison_rows,
    diagnosis_facts,
    diagnosis_notes,
    downstream_chips,
    evidence_rows,
    filter_incidents,
    PRODUCT_NAME,
    PRODUCT_TAGLINE,
    is_acknowledge_only,
    order_incidents,
    outcome_headline,
    policy_summary,
    pretty_label,
    recommendation_change_frame,
    schema_diff_rows,
    type_label,
)
from data_steward.workflow.service import StewardRuntime

configure_phoenix()

_WORDMARK = Path(__file__).resolve().parent / "assets" / "dq-doctor-wordmark.svg"
_CSS = """
<style>
    div[data-testid="stHorizontalBlock"] > div { min-width: 0; }
    .brand-name { font-size: 1.35rem; font-weight: 700; letter-spacing: -0.03em; color: #0F766E; margin: 0; }
    .brand-tag { color: #57534E; font-size: 0.85rem; margin: 0 0 0.6rem 0; }
</style>
"""


@st.cache_resource
def runtime() -> StewardRuntime:
    return StewardRuntime.from_settings()


def main() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
    if _WORDMARK.exists():
        st.logo(str(_WORDMARK), icon_image=":material/verified_user:", size="large")
    else:
        st.logo(":material/verified_user:", size="large")
    queue = st.Page(
        _queue_page,
        title="Work queue",
        icon=":material/inbox:",
        default=True,
        url_path="queue",
    )
    activity = st.Page(
        _activity_page,
        title="Activity",
        icon=":material/monitoring:",
        url_path="activity",
    )
    st.session_state["queue_page"] = queue
    st.navigation([queue, activity], position="top").run()


def _queue_page() -> None:
    steward = runtime()
    settings = get_settings()
    incidents = steward.list_incidents()
    _render_sidebar(steward, settings, incidents)
    selected_id = _selected_incident_id(incidents)
    if not selected_id:
        st.info("No incidents match these filters.")
        return
    st.session_state["incident_id"] = selected_id
    _render_workbench(steward, steward.bundle(selected_id))


def _activity_page() -> None:
    steward = runtime()
    settings = get_settings()
    incidents = steward.list_incidents()
    _render_sidebar(steward, settings, incidents, show_queue=False)
    report = steward.activity_report()
    st.subheader("Activity")
    st.caption("Counts and the immutable audit trail for this demo workspace.")
    metrics = st.columns(4)
    metrics[0].metric("Incidents", report.incidents)
    metrics[1].metric("Investigated", report.investigated)
    metrics[2].metric("Escalated", report.escalated)
    metrics[3].metric("Changed records", report.changed_records)
    metrics = st.columns(4)
    metrics[0].metric("Approved", report.approved)
    metrics[1].metric("Rejected", report.rejected)
    metrics[2].metric("Acknowledged", report.acknowledged)
    metrics[3].metric("Unauthorized writes", report.unauthorized_modifications)

    events = steward.repository.get_audit()
    st.markdown("#### Audit")
    if not events:
        st.info("No audit events yet. Investigate an incident from the work queue.")
        return
    st.dataframe(
        [
            {
                "when": event.created_at.isoformat(timespec="seconds"),
                "incident": event.incident_id,
                "action": event.action,
                "actor": event.actor,
                "proposal": event.proposal_id,
                "verified": event.verification_succeeded,
            }
            for event in events
        ],
        hide_index=True,
        width="stretch",
    )
    incident_ids = sorted({event.incident_id for event in events})
    jump_cols = st.columns([3, 1])
    jump_id = jump_cols[0].selectbox("Open incident in work queue", incident_ids)
    if jump_cols[1].button("Open", width="stretch"):
        st.session_state["incident_id"] = jump_id
        st.switch_page(st.session_state["queue_page"])


def _render_sidebar(steward: StewardRuntime, settings, incidents, *, show_queue: bool = True) -> None:
    with st.sidebar:
        st.markdown(
            f'<p class="brand-name">{PRODUCT_NAME}</p>'
            f'<p class="brand-tag">{PRODUCT_TAGLINE}</p>',
            unsafe_allow_html=True,
        )
        if show_queue:
            st.markdown("**Queue**")
            status_group = st.pills(
                "Status",
                list(STATUS_GROUPS),
                default="Needs action",
                key="queue_status",
            ) or "Needs action"
            type_group = st.pills(
                "Type",
                list(TYPE_GROUPS),
                default="All",
                key="queue_type",
            ) or "All"
            filtered = filter_incidents(
                incidents,
                status_group=status_group,
                type_group=type_group,
            )
            pinned, _ = order_incidents(incidents)
            _, rest = order_incidents(filtered)
            rest = [
                incident
                for incident in rest
                if incident.incident_id not in PINNED_INCIDENT_IDS
            ]
            selected = st.session_state.get("incident_id")
            if pinned:
                st.caption("Demo")
                for incident in pinned:
                    _queue_button(incident, selected)
            if rest:
                st.caption("Other")
                for incident in rest:
                    _queue_button(incident, selected)
            if not pinned and not rest:
                st.caption("Nothing in this filter.")
            st.divider()
        if st.button("Reset / reseed demo data", width="stretch"):
            steward.reset()
            st.session_state.pop("incident_id", None)
            st.rerun()
        st.caption("OpenAI: " + ("live" if settings.openai_api_key else "offline fallback"))
        st.caption("Arize: " + ("on" if settings.tracing_enabled else "off"))
        if settings.arize_space_id:
            st.caption(f"Project `{settings.tracing_project}`")


def _queue_button(incident, selected: str | None) -> None:
    kind = type_label(incident.incident_type)
    label = f"{incident.incident_id} · {kind} · {incident.severity} · {pretty_label(incident.status)}"
    clicked = st.button(
        label,
        key=f"queue-{incident.incident_id}",
        type="primary" if incident.incident_id == selected else "secondary",
        width="stretch",
    )
    if clicked:
        st.session_state["incident_id"] = incident.incident_id
        st.rerun()


def _selected_incident_id(incidents) -> str | None:
    current = st.session_state.get("incident_id")
    ids = {incident.incident_id for incident in incidents}
    if current in ids:
        return current
    pinned, rest = order_incidents(
        filter_incidents(
            incidents,
            status_group=st.session_state.get("queue_status") or "Needs action",
            type_group=st.session_state.get("queue_type") or "All",
        )
    )
    ordered = pinned + rest
    if ordered:
        return ordered[0].incident_id
    if incidents:
        return incidents[0].incident_id
    return None


def _render_workbench(steward: StewardRuntime, view) -> None:
    incident = view.incident
    title_col, action_col = st.columns([4, 1], vertical_alignment="bottom")
    with title_col:
        st.subheader(incident.incident_id)
        st.caption(
            " · ".join(
                part
                for part in (
                    incident.customer_id,
                    type_label(incident.incident_type),
                    pretty_label(view.status),
                    f"contract v{incident.contract_version}",
                )
                if part
            )
        )
    with action_col:
        if st.button("Run investigation", type="primary"):
            steward.investigate(incident.incident_id)
            st.session_state["incident_id"] = incident.incident_id
            st.rerun()

    badge_row = st.columns([1, 1, 1, 1, 4])
    with badge_row[0]:
        st.badge(type_label(incident.incident_type), color="blue")
    with badge_row[1]:
        st.badge(pretty_label(incident.severity), color=badge_color("severity", str(incident.severity)))
    with badge_row[2]:
        st.badge(pretty_label(view.status), color=badge_color("status", str(view.status)))
    with badge_row[3]:
        st.badge("fallback" if view.fallback_used else "live model", color="gray")

    if incident.violations:
        st.caption("Flagged: " + "; ".join(incident.violations))

    _render_evidence(view)
    _render_recommendation(view)
    _render_decision(steward, view)

    if view.remediation:
        _render_remediation(view)

    with st.expander("Investigation timeline", expanded=False):
        if view.steps:
            st.dataframe(view.steps, hide_index=True, width="stretch")
        else:
            st.caption("Run an investigation to see tool calls.")
        if view.phoenix_trace_id:
            label = view.phoenix_url or view.phoenix_trace_id
            st.markdown(f"Trace: `{label}`")

    with st.expander("Advanced / JSON", expanded=False):
        st.json(
            {
                "validation": view.validation,
                "governance": view.governance,
                "schema_changes": view.schema_changes,
                "downstream": view.downstream,
                "remediation": view.remediation,
                "human_decision": view.human_decision,
            }
        )

    if view.audit:
        with st.expander("Incident audit", expanded=False):
            for event in view.audit:
                st.write(
                    f"`{event.created_at.isoformat(timespec='seconds')}` **{event.action}** by {event.actor}"
                )


def _render_evidence(view) -> None:
    with st.container(border=True):
        st.markdown("**Evidence**")
        if view.incident.incident_type == "schema_drift":
            if view.schema_changes:
                st.dataframe(schema_diff_rows(view.schema_changes), hide_index=True, width="stretch")
                chips = downstream_chips(view.downstream)
                if chips:
                    st.markdown("**Downstream**")
                    for field, assets in chips:
                        st.markdown(f"`{field}`")
                        st.markdown(" ".join(f"`{asset}`" for asset in assets))
            else:
                st.caption("Run investigation to load the contract diff and downstream impact.")
            return
        proposed = None
        if view.recommendation and view.recommendation.candidate_value:
            proposed = view.recommendation.candidate_value
        rows = comparison_rows(view.records, proposed)
        if rows:
            st.dataframe(comparison_frame(rows), hide_index=True, width="stretch")
            st.caption("Yellow cells disagree across sources. Teal is the proposed golden value.")
        else:
            st.caption("No source records for this incident.")


def _render_recommendation(view) -> None:
    with st.container(border=True):
        recommendation = view.recommendation
        title_col, meta_col = st.columns([4, 1], vertical_alignment="bottom")
        with title_col:
            st.markdown("**Recommendation**")
        if recommendation is None:
            with meta_col:
                st.caption("—")
            st.caption("Run investigation to generate a recommendation.")
            return
        with meta_col:
            st.caption(f"Confidence {recommendation.confidence:.0%}")
        st.badge(
            pretty_label(recommendation.outcome),
            color=badge_color("outcome", recommendation.outcome),
        )
        st.markdown(f"**{outcome_headline(recommendation.outcome)}**")

        st.markdown("##### Diagnosis")
        facts = diagnosis_facts(recommendation)
        if facts:
            st.markdown("\n".join(f"- {fact}" for fact in facts))
        notes = diagnosis_notes(recommendation.rationale or "")
        if notes and not facts:
            st.markdown("\n".join(f"- {note}" for note in notes))
        elif not facts and not notes:
            st.caption("No structured diagnosis. Open the full note below.")
        with st.expander("Full model note", expanded=False):
            st.write(recommendation.rationale or "No rationale returned.")

        impact_col, uncertainty_col = st.columns(2)
        with impact_col:
            st.markdown("**Impact**")
            st.caption(recommendation.impact or "—")
        with uncertainty_col:
            st.markdown("**Uncertainty**")
            st.caption(recommendation.uncertainty or "—")

        if recommendation.changes:
            st.markdown("**Proposed golden update**")
            st.dataframe(
                recommendation_change_frame(recommendation.changes),
                hide_index=True,
                width="stretch",
            )
            st.caption("Yellow is the current golden value. Teal is the proposed write.")

        cited = evidence_rows(recommendation.evidence_refs or [])
        if cited:
            st.markdown("**Cited evidence**")
            st.dataframe(
                [
                    {
                        "Source": row["source"],
                        "Record": row["record"],
                        "Field": row["field"],
                    }
                    for row in cited
                ],
                hide_index=True,
                width="stretch",
            )

        required, policy_line = policy_summary(view.governance)
        if required:
            st.info(policy_line)
        else:
            st.caption(policy_line)
        if view.validation and not view.validation.get("valid", True):
            st.error("Policy validation failed: " + "; ".join(view.validation.get("errors") or []))


def _render_decision(steward: StewardRuntime, view) -> None:
    recommendation = view.recommendation
    if not (view.interrupted and recommendation):
        return
    with st.container(border=True):
        st.markdown("**Decision**")
        comment = st.text_area("Comment", key=f"comment-{view.incident.incident_id}")
        reviewer = st.text_input(
            "Reviewer",
            value="demo-steward",
            key=f"reviewer-{view.incident.incident_id}",
        )
        edited_changes: list[FieldChange] = []
        acknowledge_only = is_acknowledge_only(
            view.incident.incident_type, recommendation.outcome
        )
        if not acknowledge_only and recommendation.changes:
            st.caption("Edit proposed values, then use Edit and approve.")
            for change in recommendation.changes:
                new_value = st.text_input(
                    change.field,
                    value=str(change.new_value),
                    key=f"edit-{view.incident.incident_id}-{change.field}",
                )
                edited_changes.append(
                    FieldChange(field=change.field, old_value=change.old_value, new_value=new_value)
                )
        if acknowledge_only:
            st.caption("Schema changes are report-only. Acknowledge closes the incident; nothing is migrated.")
            if st.button("Acknowledge (no migration)", type="primary", width="stretch"):
                steward.decide(
                    view.incident.incident_id,
                    HumanDecision(action="acknowledge", reviewer=reviewer, comment=comment),
                )
                st.rerun()
            return
        actions = st.columns(3)
        if actions[0].button("Approve", type="primary", width="stretch"):
            steward.decide(
                view.incident.incident_id,
                HumanDecision(action="approve", reviewer=reviewer, comment=comment),
            )
            st.rerun()
        if actions[1].button("Edit and approve", width="stretch"):
            steward.decide(
                view.incident.incident_id,
                HumanDecision(
                    action="edit",
                    reviewer=reviewer,
                    comment=comment,
                    edited_changes=edited_changes,
                ),
            )
            st.rerun()
        if actions[2].button("Reject", width="stretch"):
            steward.decide(
                view.incident.incident_id,
                HumanDecision(action="reject", reviewer=reviewer, comment=comment),
            )
            st.rerun()


def _render_remediation(view) -> None:
    with st.container(border=True):
        st.markdown("**Remediation**")
        before_after = next(
            (event for event in reversed(view.audit) if event.before is not None),
            None,
        )
        pairs = (
            changed_field_pairs(before_after.before, before_after.after)
            if before_after is not None
            else []
        )
        if pairs:
            cols = st.columns(2)
            cols[0].markdown("**Before**")
            cols[0].dataframe(
                [{"field": row["field"], "value": row["before"]} for row in pairs],
                hide_index=True,
                width="stretch",
            )
            cols[1].markdown("**After**")
            cols[1].dataframe(
                [{"field": row["field"], "value": row["after"]} for row in pairs],
                hide_index=True,
                width="stretch",
            )
        result = view.remediation or {}
        st.caption(
            "Applied"
            if result.get("applied")
            else "Idempotent replay" if result.get("idempotent_replay") else "No golden write"
        )


if __name__ == "__main__":
    main()
