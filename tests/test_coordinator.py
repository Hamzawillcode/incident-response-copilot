"""End-to-end tests of the incident coordinator with one scripted model per agent.

Real ADK runs, real tool functions (backends offline, so they return error dicts), real
grounding check and real remediation on a temporary settings file. Only the models are fake.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from google.adk.sessions import InMemorySessionService

from copilot.agents.coordinator import IncidentCoordinator
from copilot.agents.team import AGENT_NAMES
from copilot.kb.documents import parse_document
from copilot.state import MemoryIncidentStore, StateError
from tests.llm_fakes import request_text, scripted

TRIAGE = {"incident_likely": True, "summary": "Checkout and payment fail.", "suspect_services": ["payment"],
          "affected_services": ["checkout", "payment"], "started_at": "", "symptoms": [],
          "search_query": "checkout errors, failures start in payment charge"}
KNOWLEDGE = {"runbooks": [{"source": "kb/runbooks/payment.md", "why_relevant": "charge failures",
                           "key_steps": ["read payment warn logs"]}],
             "past_incidents": [], "suggested_checks": [], "remediation_hints": ["roll back the payment change"],
             "note": ""}
DIAGNOSIS = {"incident": True, "root_cause_service": "payment", "failure_type": "errors", "confidence": "medium",
             "summary": "Payment charges fail; checkout only shows the errors.", "affected_services": ["checkout"],
             "evidence": [{"kind": "metric", "detail": "payment error ratio high"},
                          {"kind": "trace", "detail": "checkout failures end in payment :: charge"}],
             "alternative_causes": ["checkout"], "suggested_next_checks": [], "references": ["kb/runbooks/payment.md"]}
APPROVE = {"verdict": "approve", "summary": "Grounded and consistent.", "issues": [], "questions": []}
REVISE = {"verdict": "revise", "summary": "Timing unclear.", "issues": ["no timing evidence"],
          "questions": ["When did payment warnings start?"]}
LOOK_AT_PAYMENT = {"call": "get_service_metrics", "args": {"service": "payment", "minutes": 15}}
PROPOSE = {"call": "rollback_config", "args": {"service": "payment", "reason": "payment charges fail"}}


@pytest.fixture(autouse=True)
def _offline(failing, kb_offline):
    failing(httpx.ConnectError("no backend in unit tests"))


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "demo.flagd.json"
    path.write_text(json.dumps({"flags": {"paymentFailure": {
        "state": "ENABLED", "defaultVariant": "50%", "variants": {"off": 0, "50%": 0.5}}}}))
    monkeypatch.setenv("FLAGD_CONFIG_PATH", str(path))
    return path


def payment_failure(settings: Path) -> str:
    return json.loads(settings.read_text())["flags"]["paymentFailure"]["defaultVariant"]


def coordinator(*, store=None, session_service=None, **scripts) -> tuple[IncidentCoordinator, dict]:
    defaults = {"triage": [TRIAGE], "knowledge": [KNOWLEDGE], "diagnosis": [LOOK_AT_PAYMENT, DIAGNOSIS],
                "reviewer": [APPROVE], "action": [PROPOSE, "Rolled back the payment configuration."]}
    models = {name: scripted(*scripts.get(name, defaults[name])) for name in AGENT_NAMES}
    c = IncidentCoordinator(models=models, store=store or MemoryIncidentStore(),
                            session_service=session_service or InMemorySessionService())
    return c, models


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The happy path: investigate -> pause -> approve -> fix
# --------------------------------------------------------------------------- #
def test_full_incident_waits_for_approval_then_applies_the_fix(settings):
    c, models = coordinator()
    record = run(c.investigate("ALERT: checkout errors"))
    assert record.status == "awaiting_approval"
    assert record.proposal["tool"] == "rollback_config" and record.proposal["args"]["service"] == "payment"
    assert payment_failure(settings) == "50%"                            # nothing changed yet
    assert record.triage["suspect_services"] == ["payment"] and record.knowledge["runbooks"]
    assert record.diagnosis["root_cause_service"] == "payment"
    assert record.reviews[0]["verdict"] == "approve" and record.reviews[0]["grounding"]["hard_failures"] == []
    assert [s["agent"] for s in record.steps] == ["triage", "knowledge", "diagnosis", "reviewer", "action"]
    assert c.store.get(record.id).status == "awaiting_approval"           # persisted

    done = run(c.decide(record.id, approve=True, by="tester", note="looks right"))
    assert done.status == "fix_applied" and done.action_result["status"] == "ok"
    assert payment_failure(settings) == "off"
    assert done.decision["by"] == "tester" and done.decision["approved"] is True
    events = [t["event"] for t in done.timeline]
    assert events[:2] == ["alert", "triage"] and "proposal" in events and events.index("approved") < \
        events.index("action_result")
    assert Path(done.postmortem_path).is_file()


def test_each_agent_receives_the_earlier_agents_outputs(settings):
    c, models = coordinator()
    run(c.investigate("ALERT: checkout errors"))
    knowledge_input = request_text(models["knowledge"].requests[0])
    assert "## Triage report" in knowledge_input and "## Retrieved runbooks" in knowledge_input
    assert models["knowledge"].requests[0].config.tools in (None, [])     # no tools: one model call
    first_diagnosis = request_text(models["diagnosis"].requests[0])
    assert "## Triage report" in first_diagnosis and "## Knowledge" in first_diagnosis
    review_input = request_text(models["reviewer"].requests[0])
    assert "## Diagnosis" in review_input and "## Grounding check" in review_input
    action_input = request_text(models["action"].requests[0])
    assert "roll back the payment change" in action_input and "## Services you can act on" in action_input


def test_the_postmortem_draft_is_a_valid_knowledge_document(settings):
    c, _ = coordinator()
    record = run(c.investigate("ALERT: checkout errors"))
    done = run(c.decide(record.id, approve=True, by="tester"))
    text = Path(done.postmortem_path).read_text()
    doc = parse_document(text, kind="postmortem", source=f"kb/postmortems/{Path(done.postmortem_path).name}")
    assert doc.metadata["root_cause_service"] == "payment" and doc.metadata["status"] == "draft"
    assert "rollback_config(service='payment'" in text and "approved by tester" in text


# --------------------------------------------------------------------------- #
# Self-reflection: the review loop
# --------------------------------------------------------------------------- #
def test_reviewer_feedback_goes_back_to_the_diagnosis(settings):
    c, models = coordinator(diagnosis=[LOOK_AT_PAYMENT, DIAGNOSIS, LOOK_AT_PAYMENT, DIAGNOSIS],
                            reviewer=[REVISE, APPROVE])
    record = run(c.investigate("ALERT: checkout errors"))
    assert [r["verdict"] for r in record.reviews] == ["revise", "approve"] and len(record.diagnoses) == 2
    second_round = "\n".join(request_text(r) for r in models["diagnosis"].requests[2:])
    assert "## Reviewer feedback" in second_round and "When did payment warnings start?" in second_round
    assert record.status == "awaiting_approval"


def test_an_invented_trace_id_forces_a_revision_even_if_the_reviewer_approves(settings):
    invented = dict(DIAGNOSIS, evidence=[{"kind": "trace", "detail": f"trace {'ab' * 16} ends in payment"},
                                         {"kind": "metric", "detail": "payment error ratio high"}])
    c, models = coordinator(diagnosis=[LOOK_AT_PAYMENT, invented, LOOK_AT_PAYMENT, DIAGNOSIS],
                            reviewer=[APPROVE, APPROVE])
    record = run(c.investigate("ALERT: checkout errors"))
    assert record.reviews[0]["verdict"] == "revise"                     # grounding overrides the reviewer
    assert "ab" * 16 in " ".join(record.reviews[0]["grounding"]["hard_failures"])
    assert "not found in any tool result" in request_text(models["diagnosis"].requests[2])
    assert record.reviews[1]["verdict"] == "approve" and record.status == "awaiting_approval"


def test_unresolved_review_never_reaches_the_action_agent(settings):
    c, models = coordinator(diagnosis=[LOOK_AT_PAYMENT, DIAGNOSIS], reviewer=[REVISE])
    record = run(c.investigate("ALERT: checkout errors"))
    assert record.status == "needs_human" and len(record.reviews) == 2
    assert models["action"].requests == [] and record.proposal is None


# --------------------------------------------------------------------------- #
# Other endings
# --------------------------------------------------------------------------- #
def test_no_incident_ends_without_any_action(settings):
    calm = dict(DIAGNOSIS, incident=False, root_cause_service="none", failure_type="none",
                evidence=[{"kind": "metric", "detail": "all error ratios normal"}])
    c, models = coordinator(diagnosis=[calm], reviewer=[APPROVE])
    record = run(c.investigate("ALERT: maybe nothing"))
    assert record.status == "no_incident" and models["action"].requests == []


def test_escalation_hands_over_without_a_proposal(settings):
    escalate = {"call": "escalate_to_human", "args": {"reason": "evidence too weak"}}
    c, _ = coordinator(action=[escalate, "Escalated."])
    record = run(c.investigate("ALERT: checkout errors"))
    assert record.status == "needs_human" and record.proposal is None
    assert record.action_result["escalated"] is True and payment_failure(settings) == "50%"


def test_a_failing_knowledge_step_does_not_block_the_investigation(settings):
    c, _ = coordinator(knowledge=[RuntimeError("quota exceeded")])
    record = run(c.investigate("ALERT: checkout errors"))
    assert record.knowledge is None and any("knowledge" in e for e in record.errors)
    assert record.status == "awaiting_approval"


def test_no_diagnosis_means_failed(settings):
    c, _ = coordinator(diagnosis=[RuntimeError("quota exceeded")])
    record = run(c.investigate("ALERT: checkout errors"))
    assert record.status == "failed" and record.proposal is None


# --------------------------------------------------------------------------- #
# The human decision
# --------------------------------------------------------------------------- #
def test_rejection_changes_nothing(settings):
    c, _ = coordinator(action=[PROPOSE, "The engineer rejected it."])
    record = run(c.investigate("ALERT: checkout errors"))
    done = run(c.decide(record.id, approve=False, by="tester", note="wrong service"))
    assert done.status == "rejected" and payment_failure(settings) == "50%"
    assert done.postmortem_path is None


def test_a_decision_applies_only_once(settings):
    c, _ = coordinator()
    record = run(c.investigate("ALERT: checkout errors"))
    run(c.decide(record.id, approve=True, by="first"))
    with pytest.raises(StateError):
        run(c.decide(record.id, approve=True, by="second"))
    with pytest.raises(StateError):
        run(c.decide("inc-does-not-exist", approve=True, by="x"))


def test_a_new_coordinator_resumes_from_the_stored_session(settings):
    """Approval in another process: only the session store and the record store are shared."""
    store, sessions = MemoryIncidentStore(), InMemorySessionService()
    first, _ = coordinator(store=store, session_service=sessions)
    record = run(first.investigate("ALERT: checkout errors"))
    second, models = coordinator(store=store, session_service=sessions, action=["unused"])
    done = run(second.decide(record.id, approve=True, by="tester"))
    assert done.status == "fix_applied" and payment_failure(settings) == "off"
    assert models["action"].requests == []                              # resuming needs no model call


def test_verification_marks_the_incident_resolved(settings, monkeypatch):
    from copilot.agents import verify

    healthy = {"status": "ok", "services": [{"service": "payment", "error_pct": {"now": 0.0}},
                                            {"service": "checkout", "error_pct": {"now": 0.4}}]}
    monkeypatch.setattr(verify, "get_service_health", lambda minutes: healthy)
    c, _ = coordinator()
    record = run(c.investigate("ALERT: checkout errors"))
    done = run(c.decide(record.id, approve=True, by="tester", verify_minutes=1, verify_poll_seconds=0))
    assert done.status == "resolved" and done.verification["recovered"] is True


def test_a_quiet_triage_asks_the_diagnosis_for_a_quick_check(settings):
    quiet = dict(TRIAGE, incident_likely=False, suspect_services=[])
    calm = dict(DIAGNOSIS, incident=False, root_cause_service="none", failure_type="none",
                evidence=[{"kind": "metric", "detail": "all error ratios normal"}])
    c, models = coordinator(triage=[quiet], diagnosis=[calm], reviewer=[APPROVE])
    record = run(c.investigate("ALERT: fraud-detection 25% errors"))
    assert "found no sign of an incident" in request_text(models["diagnosis"].requests[0])
    assert record.status == "no_incident" and models["action"].requests == []


def test_a_likely_incident_gets_the_normal_task(settings):
    c, models = coordinator()
    run(c.investigate("ALERT: checkout errors"))
    assert "Investigate and give your diagnosis." in request_text(models["diagnosis"].requests[0])


def test_a_used_up_daily_quota_stops_the_investigation_at_once(settings):
    daily = RuntimeError("429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    c, models = coordinator(triage=[daily])
    record = run(c.investigate("ALERT: checkout errors"))
    assert record.status == "failed" and "daily request quota" in record.errors[-1]
    assert models["knowledge"].requests == [] and models["diagnosis"].requests == []    # no wasted steps
