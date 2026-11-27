"""Integration tests for incident state in the real Postgres (kb-postgres), in a private schema.

Run `docker compose up -d kb-postgres` first; otherwise these tests are skipped. The models are
scripted (no Gemini) and telemetry backends are offline.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import httpx
import psycopg
import pytest

from copilot.agents.coordinator import IncidentCoordinator
from copilot.agents.team import AGENT_NAMES
from copilot.kb import store as db
from copilot.state import IncidentRecord, IncidentStore, StateError, make_session_service
from tests.llm_fakes import scripted

pytestmark = pytest.mark.db

TRIAGE = {"incident_likely": True, "summary": "Checkout fails.", "suspect_services": ["payment"],
          "affected_services": ["checkout"], "started_at": "", "symptoms": [], "search_query": "checkout errors"}
KNOWLEDGE = {"runbooks": [], "past_incidents": [], "suggested_checks": [], "remediation_hints": [], "note": ""}
DIAGNOSIS = {"incident": True, "root_cause_service": "payment", "failure_type": "errors", "confidence": "medium",
             "summary": "Payment charges fail.", "affected_services": ["checkout"],
             "evidence": [{"kind": "metric", "detail": "payment error ratio high"},
                          {"kind": "trace", "detail": "failures end in payment :: charge"}]}
APPROVE = {"verdict": "approve", "summary": "ok", "issues": [], "questions": []}


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "demo.flagd.json"
    path.write_text(json.dumps({"flags": {"paymentFailure": {
        "state": "ENABLED", "defaultVariant": "50%", "variants": {"off": 0, "50%": 0.5}}}}))
    monkeypatch.setenv("FLAGD_CONFIG_PATH", str(path))
    return path


# --------------------------------------------------------------------------- #
# Incident records
# --------------------------------------------------------------------------- #
def test_incident_records_round_trip(kb_db):
    store = IncidentStore()
    record = IncidentRecord(id="inc-test-1", alert="ALERT: checkout errors")
    store.create(record)
    record.status = "awaiting_approval"
    record.diagnosis = {"root_cause_service": "payment", "failure_type": "errors"}
    record.proposal = {"tool": "rollback_config", "args": {"service": "payment"}, "confirmation_call_id": "c-1"}
    store.save(record)

    loaded = store.get("inc-test-1")
    assert loaded.status == "awaiting_approval" and loaded.proposal == record.proposal
    assert [r.id for r in store.list()] == ["inc-test-1"]
    with db.connect() as conn:
        row = conn.execute(f"SELECT root_cause_service, failure_type FROM {IncidentStore.table()}").fetchone()
    assert row == ("payment", "errors")                                 # summary columns for listing

    assert store.transition("inc-test-1", "awaiting_approval", "approved").status == "approved"
    assert store.get("inc-test-1").status == "approved"
    with pytest.raises(StateError, match="is 'approved'"):
        store.transition("inc-test-1", "awaiting_approval", "approved")
    with pytest.raises(StateError, match="not found"):
        store.get("inc-missing")
    with pytest.raises(Exception):
        store.create(record)                                              # duplicate id


def test_two_simultaneous_approvals_only_one_wins(kb_db):
    store = IncidentStore()
    record = IncidentRecord(id="inc-race", alert="ALERT", status="awaiting_approval")
    store.create(record)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def approve(name: str) -> None:
        barrier.wait()
        try:
            store.transition("inc-race", "awaiting_approval", "approved")
            outcomes.append(f"{name}: won")
        except StateError:
            outcomes.append(f"{name}: refused")

    threads = [threading.Thread(target=approve, args=(n,)) for n in ("alice", "bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(o.split(": ")[1] for o in outcomes) == ["refused", "won"]


# --------------------------------------------------------------------------- #
# ADK sessions in Postgres: approve from a "new process"
# --------------------------------------------------------------------------- #
def test_a_paused_action_is_resumed_through_a_fresh_postgres_session_service(kb_db, settings, failing):
    failing(httpx.ConnectError("no backend in unit tests"))

    def models(action_script):
        scripts = {"triage": [TRIAGE], "knowledge": [KNOWLEDGE],
                   "diagnosis": [{"call": "get_service_metrics", "args": {"service": "payment"}}, DIAGNOSIS],
                   "reviewer": [APPROVE], "action": action_script}
        return {name: scripted(*scripts[name]) for name in AGENT_NAMES}

    async def investigate():
        c = IncidentCoordinator(models=models([{"call": "rollback_config", "args": {
            "service": "payment", "reason": "charges fail"}}]), store=IncidentStore(),
            session_service=make_session_service())
        try:
            return await c.investigate("ALERT: checkout errors")
        finally:
            await c.close()

    async def approve(incident_id):
        c = IncidentCoordinator(models=models(["Rolled back."]), store=IncidentStore(),
                                session_service=make_session_service())     # nothing shared but Postgres
        try:
            return await c.decide(incident_id, approve=True, by="tester")
        finally:
            await c.close()

    record = asyncio.run(investigate())
    assert record.status == "awaiting_approval"
    assert json.loads(settings.read_text())["flags"]["paymentFailure"]["defaultVariant"] == "50%"

    done = asyncio.run(approve(record.id))
    assert done.status == "fix_applied" and done.action_result["status"] == "ok"
    assert json.loads(settings.read_text())["flags"]["paymentFailure"]["defaultVariant"] == "off"

    with psycopg.connect(db.database_url()) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (f"{kb_db}_adk",))}
        assert {"sessions", "events"} <= tables                             # ADK's own tables, own schema
        sessions = {r[0] for r in conn.execute(f"SELECT id FROM {kb_db}_adk.sessions")}
    assert f"{record.id}-action" in sessions and f"{record.id}-triage" in sessions
