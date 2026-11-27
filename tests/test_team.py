"""Unit tests for the agent team and the step runtime, driven by scripted models (no Gemini, no network).

The approval tests run ADK's real tool-confirmation flow: the action agent's write tool pauses,
and a second run_step call (a new Runner, as a new process would create) resumes it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from google.adk.sessions import InMemorySessionService

from copilot.actions import WRITE_TOOL_NAMES
from copilot.agents.runtime import (
    CONFIRMATION_TOOL, approval_message, parse_output, run_step, tool_guard,
)
from copilot.agents.team import AGENT_NAMES, MAX_TOOL_CALLS, KnowledgeReport, Review, TriageReport, build_team
from copilot.prompts import load_prompt
from copilot.tools import KB_TOOLS, TOOLS
from tests.llm_fakes import scripted

TRIAGE = {"incident_likely": True, "summary": "Checkout and payment fail.", "suspect_services": ["payment"],
          "affected_services": ["checkout", "payment"], "started_at": "",
          "symptoms": [{"service": "payment", "signal": "errors", "detail": "error_pct now 48%"}],
          "search_query": "checkout errors, failures start in payment charge"}


@pytest.fixture(autouse=True)
def _offline(failing, kb_offline):
    failing(httpx.ConnectError("no backend in unit tests"))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "demo.flagd.json"
    path.write_text(json.dumps({"flags": {"paymentFailure": {
        "state": "ENABLED", "defaultVariant": "50%", "variants": {"off": 0, "50%": 0.5}}}}))
    monkeypatch.setenv("FLAGD_CONFIG_PATH", str(path))
    return path


def team(**models):
    return build_team(models={name: models.get(name, scripted("unused")) for name in AGENT_NAMES})


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Who has which tools
# --------------------------------------------------------------------------- #
def tool_names(agent) -> set[str]:
    return {getattr(t, "name", None) or t.__name__ for t in agent.tools}


def test_each_agent_has_exactly_its_tools():
    t = team()
    assert tool_names(t.triage) == {f.__name__ for f in TOOLS}
    assert tool_names(t.knowledge) == set()                              # retrieval runs in code (D-514)
    assert tool_names(t.diagnosis) == {f.__name__ for f in TOOLS + KB_TOOLS}
    assert tool_names(t.reviewer) == set()
    assert tool_names(t.action) == WRITE_TOOL_NAMES | {"escalate_to_human"}


def test_only_the_action_agent_can_change_anything():
    for name, agent in team().agents().items():
        if name != "action":
            assert not tool_names(agent) & WRITE_TOOL_NAMES, name


def test_agents_cannot_hand_off_and_have_structured_outputs():
    t = team()
    for agent in t.agents().values():
        assert agent.disallow_transfer_to_parent and agent.disallow_transfer_to_peers and not agent.sub_agents
    assert (t.triage.output_schema, t.knowledge.output_schema, t.reviewer.output_schema) == \
        (TriageReport, KnowledgeReport, Review)
    assert t.action.output_schema is None


def test_prompts_are_used_verbatim_and_the_diagnosis_prompt_has_the_team_section():
    t = team()
    for name in ("triage", "knowledge", "reviewer", "action"):
        assert getattr(t, name).instruction(None) == load_prompt(name)
    prompt = t.diagnosis.instruction(None)
    assert prompt.startswith(load_prompt("diagnosis")) and prompt.endswith(load_prompt("diagnosis_team"))
    assert load_prompt("diagnosis_knowledge") in prompt


# --------------------------------------------------------------------------- #
# The tool guard
# --------------------------------------------------------------------------- #
def ctx(call_id: str, state: dict) -> SimpleNamespace:
    return SimpleNamespace(function_call_id=call_id, state=state)


def test_guard_enforces_allow_list_and_budget():
    guard = tool_guard(frozenset({"get_traces"}), max_calls=2)
    state: dict = {}
    assert guard(SimpleNamespace(name="rollback_config"), {}, ctx("a", state))["error"]["type"] == "forbidden_tool"
    assert guard(SimpleNamespace(name="get_traces"), {}, ctx("b", state)) is None
    assert guard(SimpleNamespace(name="get_traces"), {}, ctx("c", state)) is None
    assert guard(SimpleNamespace(name="get_traces"), {}, ctx("d", state))["error"]["type"] == "budget_exhausted"
    assert guard(SimpleNamespace(name="set_model_response"), {}, ctx("e", state)) is None


def test_guard_counts_a_resumed_call_once():
    """ADK runs the guard again when an approved call resumes; it must not use up a second call."""
    guard = tool_guard(frozenset({"rollback_config"}), max_calls=1)
    state: dict = {}
    assert guard(SimpleNamespace(name="rollback_config"), {}, ctx("call-1", state)) is None    # pauses
    assert guard(SimpleNamespace(name="rollback_config"), {}, ctx("call-1", state)) is None    # resumes
    assert guard(SimpleNamespace(name="rollback_config"), {}, ctx("call-2", state))["error"]["type"] == \
        "budget_exhausted"


def test_budgets_match_the_design():
    assert MAX_TOOL_CALLS == {"triage": 4, "knowledge": 0, "diagnosis": 10, "reviewer": 0, "action": 1}


# --------------------------------------------------------------------------- #
# run_step
# --------------------------------------------------------------------------- #
def test_run_step_parses_the_structured_output():
    llm = scripted({"call": "get_service_health", "args": {"minutes": 15}}, TRIAGE)
    svc = InMemorySessionService()
    out = run(run_step(team(triage=llm).triage, "ALERT: checkout errors", session_service=svc,
                       session_id="inc-1-triage", schema=TriageReport))
    assert out.error is None and isinstance(out.output, TriageReport)
    assert out.output.suspect_services == ["payment"] and out.tool_calls == 1
    assert out.tool_results[0]["tool"] == "get_service_health"
    assert out.tool_results[0]["response"]["status"] == "error"          # backends are offline in unit tests
    assert [s["type"] for s in out.steps] == ["tool_call", "tool_result", "output"]
    assert out.tokens["total"] == 220 and out.summary()["tools_used"] == ["get_service_health"]


def test_run_step_reports_model_failures_without_raising():
    out = run(run_step(team(triage=scripted(RuntimeError("quota exceeded"))).triage, "ALERT", session_service=
                       InMemorySessionService(), session_id="s", schema=TriageReport))
    assert out.output is None and "quota exceeded" in out.error


def test_run_step_reports_an_invalid_answer():
    """ADK itself rejects a non-JSON answer from an agent with an output schema; either way it is an error."""
    out = run(run_step(team(reviewer=scripted("looks fine to me")).reviewer, "review", session_service=
                       InMemorySessionService(), session_id="s", schema=Review))
    assert out.output is None and out.error


# --------------------------------------------------------------------------- #
# Human approval: pause, then resume in a new runner
# --------------------------------------------------------------------------- #
PROPOSE = {"call": "rollback_config", "args": {"service": "payment", "reason": "payment charges fail"}}


def test_a_write_tool_pauses_for_approval_and_changes_nothing(settings):
    before = settings.read_text()
    llm = scripted(PROPOSE, "unused")
    out = run(run_step(team(action=llm).action, "Propose one remediation.", session_service=InMemorySessionService(),
                       session_id="inc-1-action"))
    pending = out.pending_approval
    assert pending and pending["tool"] == "rollback_config"
    assert pending["args"] == {"service": "payment", "reason": "payment charges fail"}
    assert pending["confirmation_call_id"] and pending["original_call_id"]
    assert out.tool_results == []                                        # ADK's placeholder is not a result
    # ADK emits the confirmation request, then its placeholder result (core/_function_call_postprocessor.py).
    assert [s["type"] for s in out.steps] == ["tool_call", "approval_requested", "awaiting_approval"]
    assert len(llm.requests) == 1                                        # the model was not asked again
    assert settings.read_text() == before                                # nothing changed without a human


def test_an_approved_call_runs_after_resume(settings):
    svc = InMemorySessionService()
    llm = scripted(PROPOSE, "unused: no closing model call after the action (D-514)")
    agent = team(action=llm).action
    paused = run(run_step(agent, "Propose one remediation.", session_service=svc, session_id="inc-1-action"))
    resumed = run(run_step(agent, approval_message(paused.pending_approval["confirmation_call_id"], approved=True,
                                                   by="tester", incident_id="inc-1"),
                           session_service=svc, session_id="inc-1-action"))
    assert resumed.error is None
    assert resumed.tool_results[0]["tool"] == "rollback_config"
    assert resumed.tool_results[0]["response"]["status"] == "ok"
    assert json.loads(settings.read_text())["flags"]["paymentFailure"]["defaultVariant"] == "off"
    assert len(llm.requests) == 1                                        # the tool result is the answer


def test_a_rejected_call_never_runs(settings):
    svc = InMemorySessionService()
    before = settings.read_text()
    agent = team(action=scripted(PROPOSE, "The engineer rejected the rollback.")).action
    paused = run(run_step(agent, "Propose.", session_service=svc, session_id="inc-2-action"))
    resumed = run(run_step(agent, approval_message(paused.pending_approval["confirmation_call_id"], approved=False,
                                                   by="tester"), session_service=svc, session_id="inc-2-action"))
    assert "rejected" in json.dumps(resumed.tool_results[0]["response"]).lower()
    assert settings.read_text() == before


def test_approval_message_is_a_pure_function_response():
    msg = approval_message("call-7", approved=True, by="hamza", note="go", incident_id="inc-1")
    (part,) = msg.parts
    assert part.text is None and part.function_response.name == CONFIRMATION_TOOL
    assert part.function_response.id == "call-7"
    assert part.function_response.response == {"confirmed": True,
                                               "payload": {"by": "hamza", "note": "go", "incident_id": "inc-1"}}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [TRIAGE, json.dumps(TRIAGE), "```json\n" + json.dumps(TRIAGE) + "\n```",
                                   "Here: " + json.dumps(TRIAGE) + " done"])
def test_parse_output_accepts_common_shapes(value):
    assert parse_output(TriageReport, value).suspect_services == ["payment"]


@pytest.mark.parametrize("value", [None, "", "no json", "{broken", {"summary": "missing fields"}])
def test_parse_output_rejects_bad_answers(value):
    assert parse_output(TriageReport, value) is None
