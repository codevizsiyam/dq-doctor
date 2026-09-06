from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.types import Command

from data_steward.config import Settings, get_settings
from data_steward.models import (
    ActivityReport,
    HumanDecision,
    Incident,
    Recommendation,
    SourceRecord,
)
from data_steward.observability.phoenix import configure_phoenix, phoenix_trace_url, trace_incident
from data_steward.remediation import RemediationService
from data_steward.reports import build_activity_report
from data_steward.seed import reset_database
from data_steward.sqlite import SQLiteRepository
from data_steward.tools import load_contract
from data_steward.workflow.graph import WorkflowContext, build_graph
from data_steward.workflow.toolkit import InvestigationToolkit


@dataclass
class RunView:
    incident: Incident
    records: list[SourceRecord]
    recommendation: Recommendation | None
    validation: dict[str, Any] | None
    governance: dict[str, Any] | None
    steps: list[dict[str, Any]]
    human_decision: dict[str, Any] | None
    remediation: dict[str, Any] | None
    status: str
    interrupted: bool
    proposal_id: str | None
    phoenix_trace_id: str | None
    phoenix_url: str | None
    schema_changes: list[dict[str, Any]]
    downstream: dict[str, list[str]]
    fallback_used: bool
    audit: list[Any]


class StewardRuntime:
    def __init__(
        self,
        repository: SQLiteRepository,
        settings: Settings,
        *,
        force_fallback: bool = False,
        checkpointer: Any | None = None,
    ) -> None:
        configure_phoenix(settings)
        self.repository = repository
        self.settings = settings
        self.contract = load_contract(settings.contract_path)
        self.toolkit = InvestigationToolkit(repository, self.contract, settings)
        self.remediator = RemediationService(repository, self.contract)
        self.context = WorkflowContext(
            repository=repository,
            contract=self.contract,
            settings=settings,
            toolkit=self.toolkit,
            remediator=self.remediator,
            force_fallback=force_fallback,
        )
        self._checkpoint_conn = None
        if checkpointer is None:
            checkpointer, self._checkpoint_conn = _sqlite_checkpointer(settings.checkpoint_path)
        self.graph = build_graph(self.context, checkpointer)

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs: Any) -> "StewardRuntime":
        settings = settings or get_settings()
        repository = SQLiteRepository(settings.database_path)
        if not settings.database_path.exists():
            reset_database(settings.database_path)
        else:
            repository.initialize()
        return cls(repository, settings, **kwargs)

    def reset(self) -> None:
        reset_database(self.settings.database_path)
        if self.settings.checkpoint_path.exists():
            self.settings.checkpoint_path.unlink()
        checkpointer, self._checkpoint_conn = _sqlite_checkpointer(self.settings.checkpoint_path)
        self.graph = build_graph(self.context, checkpointer)

    def list_incidents(self) -> list[Incident]:
        return self.repository.list_incidents()

    def activity_report(self) -> ActivityReport:
        return build_activity_report(self.repository)

    def investigate(self, incident_id: str) -> RunView:
        config = _thread(incident_id)
        snapshot = self.graph.get_state(config)
        if snapshot.next:
            return self._view(incident_id, snapshot, interrupted=True)
        with trace_incident(incident_id, operation="investigate"):
            self.graph.invoke({"incident_id": incident_id}, config)
        snapshot = self.graph.get_state(config)
        return self._view(incident_id, snapshot, interrupted=bool(snapshot.next))

    def decide(self, incident_id: str, decision: HumanDecision) -> RunView:
        config = _thread(incident_id)
        with trace_incident(incident_id, operation="resume", decision=decision.action):
            self.graph.invoke(Command(resume=decision.model_dump(mode="json")), config)
        snapshot = self.graph.get_state(config)
        return self._view(incident_id, snapshot, interrupted=bool(snapshot.next))

    def bundle(self, incident_id: str) -> RunView:
        snapshot = self.graph.get_state(_thread(incident_id))
        return self._view(incident_id, snapshot, interrupted=bool(snapshot.next))

    def _view(self, incident_id: str, snapshot: Any, *, interrupted: bool) -> RunView:
        incident = self.repository.get_incident(incident_id)
        if incident is None:
            raise ValueError(f"unknown incident {incident_id}")
        values = snapshot.values or {}
        recommendation = values.get("recommendation")
        trace_id = values.get("phoenix_trace_id")
        customer_id = incident.customer_id
        records = self.repository.find_records(customer_id) if customer_id else []
        return RunView(
            incident=incident,
            records=records,
            recommendation=(
                Recommendation.model_validate(recommendation) if recommendation else None
            ),
            validation=values.get("validation"),
            governance=values.get("governance"),
            steps=list(values.get("tool_trace") or []),
            human_decision=values.get("human_decision"),
            remediation=values.get("remediation"),
            status=values.get("status") or incident.status,
            interrupted=interrupted,
            proposal_id=values.get("proposal_id"),
            phoenix_trace_id=trace_id,
            phoenix_url=phoenix_trace_url(trace_id),
            schema_changes=list(values.get("schema_changes") or []),
            downstream=dict(values.get("downstream") or {}),
            fallback_used=bool(values.get("fallback_used")),
            audit=self.repository.get_audit(incident_id),
        )


def _thread(incident_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": incident_id}}


def _sqlite_checkpointer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        connection = sqlite3.connect(path, check_same_thread=False)
        return SqliteSaver(connection), connection
    except Exception:
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver(), None
