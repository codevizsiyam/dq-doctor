from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from data_steward.models import (
    Approval,
    AuditEvent,
    Incident,
    LineageEdge,
    Proposal,
    Severity,
    SourceRecord,
)

SEVERITY_RANK = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}

ModelT = TypeVar("ModelT", bound=BaseModel)


class SQLiteRepository:
    """Small SQLite persistence boundary; domain objects are stored as validated JSON."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS records (
          source TEXT NOT NULL, record_id TEXT NOT NULL, customer_id TEXT NOT NULL,
          payload TEXT NOT NULL, PRIMARY KEY (source, record_id)
        );
        CREATE INDEX IF NOT EXISTS records_customer_idx ON records(customer_id);
        CREATE TABLE IF NOT EXISTS incidents (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proposals (
          id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approvals (
          id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lineage (
          id TEXT PRIMARY KEY, target_record_id TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, proposal_id TEXT,
          created_at TEXT NOT NULL, payload TEXT NOT NULL
        );
        """
        with self._connect() as connection:
            connection.executescript(schema)

    def reset(self) -> None:
        with self._connect() as connection:
            for table in ("audit", "lineage", "approvals", "proposals", "incidents", "records"):
                connection.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names

    @staticmethod
    def _json(model: BaseModel) -> str:
        return json.dumps(model.model_dump(mode="json"), sort_keys=True)

    @staticmethod
    def _decode(row: sqlite3.Row | None, model: type[ModelT]) -> ModelT | None:
        return model.model_validate_json(row["payload"]) if row else None

    def save_record(self, record: SourceRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO records(source, record_id, customer_id, payload)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source, record_id) DO UPDATE SET
                   customer_id=excluded.customer_id, payload=excluded.payload""",
                (record.source, record.record_id, record.customer_id, self._json(record)),
            )

    def get_record(self, source: str, record_id: str) -> SourceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM records WHERE source=? AND record_id=?",
                (source, record_id),
            ).fetchone()
        return self._decode(row, SourceRecord)

    def find_records(self, customer_id: str) -> list[SourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM records WHERE customer_id=? ORDER BY source, record_id",
                (customer_id,),
            ).fetchall()
        return [SourceRecord.model_validate_json(row["payload"]) for row in rows]

    def save_incident(self, incident: Incident) -> None:
        self._upsert("incidents", incident.incident_id, incident)

    def get_incident(self, incident_id: str) -> Incident | None:
        return self._get("incidents", incident_id, Incident)

    def list_incidents(self) -> list[Incident]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM incidents").fetchall()
        incidents = [Incident.model_validate_json(row["payload"]) for row in rows]
        return sorted(
            incidents,
            key=lambda incident: (
                SEVERITY_RANK.get(incident.severity, 9),
                incident.incident_id,
            ),
        )

    def save_proposal(self, proposal: Proposal) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO proposals(id, incident_id, payload) VALUES (?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""",
                (proposal.proposal_id, proposal.incident_id, self._json(proposal)),
            )

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        return self._get("proposals", proposal_id, Proposal)

    def list_proposals(self, incident_id: str | None = None) -> list[Proposal]:
        query = "SELECT payload FROM proposals"
        params: tuple[str, ...] = ()
        if incident_id is not None:
            query += " WHERE incident_id=?"
            params = (incident_id,)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [Proposal.model_validate_json(row["payload"]) for row in rows]

    def save_approval(self, approval: Approval) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO approvals(id, proposal_id, payload) VALUES (?, ?, ?)
                   ON CONFLICT(proposal_id) DO UPDATE SET
                   id=excluded.id, payload=excluded.payload""",
                (approval.approval_id, approval.proposal_id, self._json(approval)),
            )

    def get_approval(self, proposal_id: str) -> Approval | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM approvals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return self._decode(row, Approval)

    def list_approvals(self) -> list[Approval]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM approvals").fetchall()
        return [Approval.model_validate_json(row["payload"]) for row in rows]

    def save_lineage(self, edge: LineageEdge) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO lineage(id, target_record_id, payload) VALUES (?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""",
                (edge.lineage_id, edge.target_record_id, self._json(edge)),
            )

    def get_lineage(self, target_record_id: str) -> list[LineageEdge]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM lineage WHERE target_record_id=? ORDER BY id",
                (target_record_id,),
            ).fetchall()
        return [LineageEdge.model_validate_json(row["payload"]) for row in rows]

    def append_audit(self, event: AuditEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO audit
                   (id, incident_id, proposal_id, created_at, payload) VALUES (?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.incident_id,
                    event.proposal_id,
                    event.created_at.isoformat(),
                    self._json(event),
                ),
            )

    def get_audit(self, incident_id: str | None = None) -> list[AuditEvent]:
        with self._connect() as connection:
            if incident_id is None:
                rows = connection.execute(
                    "SELECT payload FROM audit ORDER BY created_at, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM audit WHERE incident_id=? ORDER BY created_at, id",
                    (incident_id,),
                ).fetchall()
        return [AuditEvent.model_validate_json(row["payload"]) for row in rows]

    def _upsert(self, table: str, identifier: str, model: BaseModel) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""INSERT INTO {table}(id, payload) VALUES (?, ?)
                    ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""",
                (identifier, self._json(model)),
            )

    def _get(self, table: str, identifier: str, model: type[ModelT]) -> ModelT | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE id=?", (identifier,)
            ).fetchone()
        return self._decode(row, model)
