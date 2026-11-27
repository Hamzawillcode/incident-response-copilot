"""Incident state (Week 4): incident records and ADK sessions, both in the kb-postgres database.

Two kinds of state, one database:

* **Incident records** (this module, table ``<KB_SCHEMA>.incidents``): one row per incident
  with its status and the full record as JSON: the alert, every agent's structured output,
  the proposed action, the human decision, the action result and the verification.
  The approval step is a compare-and-set (``UPDATE ... WHERE status = 'awaiting_approval'``),
  so a decision is applied exactly once, even if two people approve at the same moment.
* **ADK sessions** (``DatabaseSessionService``, its own schema ``adk``): every event of every
  agent step. This is what lets a *different process* resume a paused action after a human
  approves it hours later.

``MemoryIncidentStore`` and ``COPILOT_SESSIONS=memory`` give the same behaviour in memory
(unit tests, quick experiments).
"""

from __future__ import annotations

import copy
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from copilot.kb import store as db
from copilot.kb.store import StoreError, translate_errors

__all__ = ["APP_NAME", "FINAL_STATUSES", "IncidentRecord", "IncidentStore", "MemoryIncidentStore", "StateError",
           "USER_ID", "adk_schema", "make_session_service", "new_incident_id", "now_iso", "result_message"]

APP_NAME = "incident-copilot"
USER_ID = "oncall"

#: investigating -> awaiting_approval -> approved -> fix_applied -> resolved
#:                                    -> rejected
#:               -> no_incident | needs_human | failed
STATUSES = ("investigating", "awaiting_approval", "approved", "rejected", "fix_applied", "resolved",
            "no_incident", "needs_human", "failed")
FINAL_STATUSES = frozenset({"rejected", "resolved", "no_incident", "needs_human", "failed"})
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class StateError(Exception):
    """An incident does not exist, or is not in the status an operation needs."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def result_message(result: dict[str, Any] | None) -> str:
    """The human-readable message of a tool result.

    Our tools put it in ``message`` or ``error.message``; ADK's own answers use a plain
    string ``error`` (e.g. ``{"error": "This tool call is rejected."}``).
    """
    if not isinstance(result, dict):
        return ""
    if result.get("message"):
        return str(result["message"])
    error = result.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "")
    return str(error or "")


def new_incident_id() -> str:
    """inc-20261128-0a1b2c (sortable by day, unique enough for a team)."""
    return f"inc-{datetime.now(timezone.utc):%Y%m%d}-{os.urandom(3).hex()}"


class IncidentRecord(BaseModel):
    """Everything known about one incident. Stored as JSON; every field is plain data."""

    id: str
    alert: str
    status: str = "investigating"
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    timeline: list[dict[str, Any]] = Field(default_factory=list)      # [{"at", "event", "detail"}]
    triage: dict[str, Any] | None = None
    retrieval: dict[str, Any] | None = None                            # what the KB search returned
    knowledge: dict[str, Any] | None = None
    diagnoses: list[dict[str, Any]] = Field(default_factory=list)      # one per review round
    reviews: list[dict[str, Any]] = Field(default_factory=list)        # {"review", "grounding", "verdict"}
    diagnosis: dict[str, Any] | None = None                            # the final one
    proposal: dict[str, Any] | None = None                             # the paused action call
    decision: dict[str, Any] | None = None                             # {"approved", "by", "note", "at"}
    action_result: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    postmortem_path: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)          # per agent step: tokens, latency, errors
    errors: list[str] = Field(default_factory=list)

    def note(self, event: str, detail: str = "") -> None:
        self.timeline.append({"at": now_iso(), "event": event, "detail": detail})

    def totals(self) -> dict[str, Any]:
        return {
            "agent_steps": len(self.steps),
            "llm_calls": sum(s.get("llm_calls", 0) for s in self.steps),
            "tool_calls": sum(s.get("tool_calls", 0) for s in self.steps),
            "tokens": sum(s.get("tokens", {}).get("total", 0) for s in self.steps),
            "latency_seconds": round(sum(s.get("latency_seconds", 0.0) for s in self.steps), 2),
            "rate_limit_wait_seconds": round(sum(s.get("rate_limit_wait_seconds", 0.0) for s in self.steps), 1),
        }


# --------------------------------------------------------------------------- #
# Incident stores
# --------------------------------------------------------------------------- #
class MemoryIncidentStore:
    """In-memory store with the same semantics as the Postgres one (for tests)."""

    def __init__(self) -> None:
        self._records: dict[str, IncidentRecord] = {}
        self._lock = threading.Lock()

    def create(self, record: IncidentRecord) -> None:
        with self._lock:
            if record.id in self._records:
                raise StateError(f"Incident {record.id} already exists.")
            self._records[record.id] = copy.deepcopy(record)

    def save(self, record: IncidentRecord) -> None:
        record.updated_at = now_iso()
        with self._lock:
            if record.id not in self._records:
                raise StateError(f"Incident {record.id} does not exist.")
            self._records[record.id] = copy.deepcopy(record)

    def get(self, incident_id: str) -> IncidentRecord:
        with self._lock:
            if incident_id not in self._records:
                raise StateError(f"Incident {incident_id} not found.")
            return copy.deepcopy(self._records[incident_id])

    def transition(self, incident_id: str, expected: str, new: str) -> IncidentRecord:
        """Compare-and-set the status. Raises StateError if the status is not ``expected``."""
        with self._lock:
            record = self._records.get(incident_id)
            if record is None:
                raise StateError(f"Incident {incident_id} not found.")
            if record.status != expected:
                raise StateError(f"Incident {incident_id} is '{record.status}', not '{expected}'.")
            record.status = new
            record.updated_at = now_iso()
            return copy.deepcopy(record)

    def list(self, limit: int = 20) -> list[IncidentRecord]:
        with self._lock:
            records = sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)
            return [copy.deepcopy(r) for r in records[:limit]]


class IncidentStore:
    """Incident records in Postgres (``<KB_SCHEMA>.incidents``)."""

    _DDL = """
    CREATE TABLE IF NOT EXISTS {table} (
        id                 text PRIMARY KEY,
        status             text NOT NULL,
        alert              text NOT NULL,
        root_cause_service text,
        failure_type       text,
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now(),
        record             jsonb NOT NULL
    )"""

    def __init__(self) -> None:
        self._ready = False

    @staticmethod
    def table() -> str:
        return f"{db.schema_name()}.incidents"

    def _connect(self):
        conn = db.connect(read_only=False)
        if not self._ready:
            with translate_errors():
                conn.execute(self._DDL.format(table=self.table()))
            self._ready = True
        return conn

    def create(self, record: IncidentRecord) -> None:
        with self._connect() as conn, translate_errors():
            conn.execute(f"INSERT INTO {self.table()} (id, status, alert, record) VALUES (%s, %s, %s, %s)",
                         (record.id, record.status, record.alert, Jsonb(record.model_dump(mode="json"))))

    def save(self, record: IncidentRecord) -> None:
        record.updated_at = now_iso()
        diagnosis = record.diagnosis or {}
        with self._connect() as conn, translate_errors():
            cur = conn.execute(
                f"UPDATE {self.table()} SET status = %s, root_cause_service = %s, failure_type = %s, "
                "updated_at = now(), record = %s WHERE id = %s",
                (record.status, diagnosis.get("root_cause_service"), diagnosis.get("failure_type"),
                 Jsonb(record.model_dump(mode="json")), record.id))
            if cur.rowcount != 1:
                raise StateError(f"Incident {record.id} does not exist.")

    def get(self, incident_id: str) -> IncidentRecord:
        with self._connect() as conn, translate_errors():
            row = conn.execute(f"SELECT status, record FROM {self.table()} WHERE id = %s", (incident_id,)).fetchone()
        if row is None:
            raise StateError(f"Incident {incident_id} not found.")
        record = IncidentRecord.model_validate(row[1])
        record.status = row[0]  # the column is the source of truth for the status
        return record

    def transition(self, incident_id: str, expected: str, new: str) -> IncidentRecord:
        """Compare-and-set: exactly one caller can move an incident out of ``expected``."""
        with self._connect() as conn, translate_errors():
            row = conn.execute(
                f"UPDATE {self.table()} SET status = %s, updated_at = now(), "
                "record = jsonb_set(record, '{status}', to_jsonb(%s::text)) "
                "WHERE id = %s AND status = %s RETURNING record",
                (new, new, incident_id, expected)).fetchone()
            if row is None:
                current = conn.execute(f"SELECT status FROM {self.table()} WHERE id = %s",
                                       (incident_id,)).fetchone()
        if row is None:
            if current is None:
                raise StateError(f"Incident {incident_id} not found.")
            raise StateError(f"Incident {incident_id} is '{current[0]}', not '{expected}'.")
        record = IncidentRecord.model_validate(row[0])
        record.status = new
        return record

    def list(self, limit: int = 20) -> list[IncidentRecord]:
        with self._connect() as conn, translate_errors():
            rows = conn.execute(f"SELECT status, record FROM {self.table()} ORDER BY created_at DESC LIMIT %s",
                                (limit,)).fetchall()
        records = []
        for status, data in rows:
            record = IncidentRecord.model_validate(data)
            record.status = status
            records.append(record)
        return records


# --------------------------------------------------------------------------- #
# ADK sessions
# --------------------------------------------------------------------------- #
def adk_schema() -> str:
    schema = os.environ.get("COPILOT_ADK_SCHEMA", "").strip() or "adk"
    if not _IDENTIFIER.fullmatch(schema):
        raise StoreError("configuration", "COPILOT_ADK_SCHEMA must be a lowercase SQL identifier.")
    return schema


def make_session_service():
    """ADK session service: Postgres (default) or in memory (``COPILOT_SESSIONS=memory``).

    ADK's tables (sessions, events, app/user state) live in their own schema so they cannot be
    confused with ours. SQLAlchemy uses the psycopg 3 driver in asyncio mode
    (``postgresql+psycopg://``), the same driver as the rest of the copilot.
    """
    if os.environ.get("COPILOT_SESSIONS", "").strip().lower() == "memory":
        from google.adk.sessions import InMemorySessionService
        return InMemorySessionService()

    from google.adk.sessions import DatabaseSessionService

    schema = adk_schema()
    with db.connect(read_only=False) as conn, translate_errors():
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    return DatabaseSessionService(db_url=sqlalchemy_url(db.database_url()),
                                  connect_args={"options": f"-c search_path={schema}"})


def sqlalchemy_url(url: str) -> str:
    """postgresql://u:p@h/db -> postgresql+psycopg://u:p@h/db (async-capable driver)."""
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.split("+")[0] not in ("postgresql", "postgres"):
        raise StoreError("configuration", "KB_DATABASE_URL must be a postgresql:// URL.")
    return f"postgresql+psycopg://{rest}"
