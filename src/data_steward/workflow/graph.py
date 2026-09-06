from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from data_steward.config import Settings
from data_steward.governance import assess_governance, validate_proposal
from data_steward.models import (
    Approval,
    AuditEvent,
    DataContract,
    Decision,
    FieldChange,
    HumanDecision,
    Incident,
    IncidentStatus,
    Recommendation,
)
from data_steward.observability.phoenix import current_trace_id
from data_steward.remediation import RemediationService
from data_steward.repositories import Repository
from data_steward.workflow.investigator import run_investigator
from data_steward.workflow.state import StewardState
from data_steward.workflow.toolkit import InvestigationToolkit


@dataclass
class WorkflowContext:
    repository: Repository
    contract: DataContract
    settings: Settings
    toolkit: InvestigationToolkit
    remediator: RemediationService
    force_fallback: bool = False


def build_graph(context: WorkflowContext, checkpointer: Any):
    graph = StateGraph(StewardState)
    graph.add_node("load_incident", lambda state: _load_incident(context, state))
    graph.add_node("investigate", lambda state: _investigate(context, state))
    graph.add_node("validate_policy", lambda state: _validate_policy(context, state))
    graph.add_node("human_review", _human_review)
    graph.add_node("apply_decision", lambda state: _apply_decision(context, state))
    graph.add_edge(START, "load_incident")
    graph.add_edge("load_incident", "investigate")
    graph.add_edge("investigate", "validate_policy")
    graph.add_edge("validate_policy", "human_review")
    graph.add_edge("human_review", "apply_decision")
    graph.add_edge("apply_decision", END)
    return graph.compile(checkpointer=checkpointer)


def _coerce_schema_report(incident: Incident, recommendation: Recommendation) -> Recommendation:
    """Schema tickets are report-only. Live models sometimes emit recommend + changes."""
    if incident.incident_type != "schema_drift":
        return recommendation
    return recommendation.model_copy(
        update={
            "outcome": "report_only",
            "classified_type": "schema_drift",
            "changes": [],
            "candidate_value": {"migration": "none"},
        }
    )


def _load_incident(context: WorkflowContext, state: StewardState) -> dict[str, Any]:
    incident = context.repository.get_incident(state["incident_id"])
    if incident is None:
        raise ValueError(f"unknown incident {state['incident_id']}")
    context.repository.save_incident(
        incident.model_copy(update={"status": IncidentStatus.INVESTIGATING})
    )
    return {
        "incident": incident.model_dump(mode="json"),
        "classified_type": incident.incident_type,
        "status": IncidentStatus.INVESTIGATING.value,
        "phoenix_trace_id": current_trace_id(),
        "error": None,
    }


def _investigate(context: WorkflowContext, state: StewardState) -> dict[str, Any]:
    incident = Incident.model_validate(state["incident"])
    recommendation, steps, extras = run_investigator(
        incident,
        context.toolkit,
        force_fallback=context.force_fallback,
    )
    recommendation = _coerce_schema_report(incident, recommendation)
    status = {
        "recommend": IncidentStatus.AWAITING_APPROVAL,
        "escalate": IncidentStatus.ESCALATED,
        "report_only": IncidentStatus.AWAITING_APPROVAL,
    }[recommendation.outcome]
    context.repository.save_incident(incident.model_copy(update={"status": status}))
    proposal = recommendation.to_proposal(f"PROP-{incident.incident_id}")
    if proposal is not None:
        context.repository.save_proposal(proposal)
    context.repository.append_audit(
        AuditEvent(
            event_id=f"investigate:{incident.incident_id}",
            incident_id=incident.incident_id,
            proposal_id=proposal.proposal_id if proposal else None,
            action="investigation_completed",
            actor="investigator",
            details={
                "outcome": recommendation.outcome,
                "tools": recommendation.tools_used,
                "fallback_used": recommendation.fallback_used,
                "classified_type": recommendation.classified_type,
                "phoenix_trace_id": state.get("phoenix_trace_id"),
            },
        )
    )
    return {
        "recommendation": recommendation.model_dump(mode="json"),
        "tool_trace": steps,
        "step_count": len(steps),
        "proposal_id": proposal.proposal_id if proposal else None,
        "schema_changes": extras.get("schema_changes") or [],
        "downstream": extras.get("downstream") or {},
        "fallback_used": recommendation.fallback_used,
        "classified_type": recommendation.classified_type,
        "status": status.value,
    }


def _validate_policy(context: WorkflowContext, state: StewardState) -> dict[str, Any]:
    recommendation = Recommendation.model_validate(state["recommendation"])
    proposal = None
    if state.get("proposal_id"):
        proposal = context.repository.get_proposal(state["proposal_id"])
    target = None
    if proposal is not None:
        target = context.repository.get_record("golden", proposal.target_record_id)
        validation = validate_proposal(proposal, target, context.contract)
        if not validation.valid:
            recommendation = recommendation.model_copy(
                update={
                    "outcome": "escalate",
                    "uncertainty": "Deterministic validation rejected the proposal.",
                    "rationale": recommendation.rationale
                    + " Validation errors: "
                    + "; ".join(validation.errors),
                }
            )
            incident = Incident.model_validate(state["incident"])
            context.repository.save_incident(
                incident.model_copy(update={"status": IncidentStatus.ESCALATED})
            )
            return {
                "recommendation": recommendation.model_dump(mode="json"),
                "validation": validation.model_dump(mode="json"),
                "proposal_id": None,
                "governance": assess_governance(
                    proposal,
                    context.contract,
                    context.settings.approval_confidence_threshold,
                    material_evidence_conflict=True,
                ).model_dump(mode="json"),
                "status": IncidentStatus.ESCALATED.value,
            }
        governance = assess_governance(
            proposal,
            context.contract,
            context.settings.approval_confidence_threshold,
            material_evidence_conflict=recommendation.outcome == "escalate",
        )
    else:
        from data_steward.models import ValidationResult, GovernanceAssessment

        validation = ValidationResult(valid=True, warnings=["no golden-record write proposed"])
        governance = GovernanceAssessment(
            approval_required=True,
            reasons=["human confirmation required"],
            risk="high" if recommendation.outcome != "recommend" else "medium",
        )
    return {
        "recommendation": recommendation.model_dump(mode="json"),
        "validation": validation.model_dump(mode="json"),
        "governance": governance.model_dump(mode="json"),
        "status": (
            IncidentStatus.ESCALATED.value
            if recommendation.outcome == "escalate"
            else state.get("status")
        ),
    }


def _human_review(state: StewardState) -> dict[str, Any]:
    decision = interrupt(
        {
            "incident_id": state["incident_id"],
            "recommendation": state.get("recommendation"),
            "validation": state.get("validation"),
            "governance": state.get("governance"),
            "proposal_id": state.get("proposal_id"),
        }
    )
    return {"human_decision": decision}


def _apply_decision(context: WorkflowContext, state: StewardState) -> dict[str, Any]:
    decision = HumanDecision.model_validate(state["human_decision"])
    incident = context.repository.get_incident(state["incident_id"])
    if incident is None:
        raise ValueError("incident disappeared before approval")
    proposal_id = state.get("proposal_id")
    recommendation = Recommendation.model_validate(state["recommendation"])

    if decision.action == "reject":
        _store_decision(context, proposal_id, decision, Decision.REJECTED)
        context.repository.save_incident(
            incident.model_copy(update={"status": IncidentStatus.REJECTED})
        )
        context.repository.append_audit(
            _decision_event(incident.incident_id, proposal_id, decision, "rejected")
        )
        return {"status": IncidentStatus.REJECTED.value, "remediation": None}

    if (
        decision.action == "acknowledge"
        or recommendation.outcome == "report_only"
        or incident.incident_type == "schema_drift"
        or not proposal_id
    ):
        _store_decision(context, proposal_id, decision, Decision.APPROVED)
        context.repository.save_incident(
            incident.model_copy(update={"status": IncidentStatus.ACKNOWLEDGED})
        )
        context.repository.append_audit(
            _decision_event(incident.incident_id, proposal_id, decision, "acknowledged")
        )
        return {"status": IncidentStatus.ACKNOWLEDGED.value, "remediation": None}

    approved_changes = decision.edited_changes or None
    if decision.action == "edit" and decision.edited_changes:
        proposal = context.repository.get_proposal(proposal_id)
        if proposal is not None:
            context.repository.save_proposal(
                proposal.model_copy(update={"changes": decision.edited_changes})
            )
    _store_decision(context, proposal_id, decision, Decision.APPROVED, approved_changes)
    context.repository.append_audit(
        _decision_event(incident.incident_id, proposal_id, decision, decision.action)
    )
    result = context.remediator.apply(proposal_id)
    return {
        "status": IncidentStatus.RESOLVED.value if result.applied or result.idempotent_replay else incident.status,
        "remediation": {
            "applied": result.applied,
            "idempotent_replay": result.idempotent_replay,
            "verification": result.verification.model_dump(mode="json"),
            "record": result.record.model_dump(mode="json"),
        },
    }


def _store_decision(
    context: WorkflowContext,
    proposal_id: str | None,
    decision: HumanDecision,
    mapped: Decision,
    approved_changes: list[FieldChange] | None = None,
) -> None:
    if not proposal_id:
        return
    context.repository.save_approval(
        Approval(
            approval_id=f"APR-{proposal_id}",
            proposal_id=proposal_id,
            decision=mapped,
            reviewer=decision.reviewer,
            comment=decision.comment or None,
            approved_changes=approved_changes,
        )
    )


def _decision_event(
    incident_id: str,
    proposal_id: str | None,
    decision: HumanDecision,
    action: str,
) -> AuditEvent:
    return AuditEvent(
        event_id=f"decision:{incident_id}:{action}",
        incident_id=incident_id,
        proposal_id=proposal_id,
        action=action,
        actor=decision.reviewer,
        details={"comment": decision.comment, "human_action": decision.action},
    )
