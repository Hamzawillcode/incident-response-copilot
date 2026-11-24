"""Unit tests for the diagnosis agent, driven by a scripted fake model (no Gemini, no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from google.adk.models import BaseLlm, LlmCapabilities, LlmResponse
from google.genai import types

from copilot.agents import diagnosis as dx
from copilot.agents.console import format_step
from copilot.agents.diagnosis import Diagnosis, build_diagnosis_agent, diagnose, parse_diagnosis
from copilot.prompts import load_prompt
from copilot.tools import TOOLS

FINAL = {
    "incident": True, "root_cause_service": "payment", "failure_type": "errors", "confidence": "high",
    "summary": "Payment charge calls fail; checkout errors are a downstream symptom.",
    "affected_services": ["checkout", "frontend"],
    "evidence": [{"kind": "trace", "detail": "error_origins payment :: charge x6"},
                 {"kind": "log", "detail": "payment warn 'Payment request failed' x6"}],
    "alternative_causes": ["checkout"], "suggested_next_checks": ["recent payment deploys"],
}


class ScriptedLlm(BaseLlm):
    """Plays back a fixed list of model turns: function-call dicts, then the final JSON text.

    ``native_schema=False`` behaves like Gemini via an AI Studio key: ADK then adds
    its ``set_model_response`` tool and the model hands in the answer by calling it.
    """

    model: str = "scripted"
    script: list = []
    requests: list = []
    native_schema: bool = True

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=self.native_schema)

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        turn = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, dict) and "call" in turn:
            part = types.Part(function_call=types.FunctionCall(name=turn["call"], args=turn.get("args", {})))
        else:
            part = types.Part(text=turn if isinstance(turn, str) else json.dumps(turn))
        usage = types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=10,
                                                           total_token_count=110)
        yield LlmResponse(content=types.Content(role="model", parts=[part]), usage_metadata=usage)


@pytest.fixture(autouse=True)
def _offline_backends(failing):
    failing(httpx.ConnectError("no backend in unit tests"))   # tools return status=error, never hit the network


def scripted(*turns) -> ScriptedLlm:
    return ScriptedLlm(script=list(turns), requests=[])


# --------------------------------------------------------------------------- #
# Construction and safety
# --------------------------------------------------------------------------- #
def test_agent_has_exactly_the_read_only_tools():
    agent = build_diagnosis_agent(model=scripted(FINAL))
    names = [t.__name__ for t in agent.tools]
    assert names == [t.__name__ for t in TOOLS]
    assert set(names) == {"get_service_health", "get_service_metrics", "get_traces", "search_logs", "query_metrics"}
    for name in names:
        assert not any(w in name for w in ("flag", "http", "shell", "exec", "write", "set_", "delete"))


def test_agent_cannot_hand_off_to_other_agents():
    agent = build_diagnosis_agent(model=scripted(FINAL))
    assert agent.disallow_transfer_to_parent and agent.disallow_transfer_to_peers and not agent.sub_agents


def test_model_comes_from_environment(monkeypatch):
    monkeypatch.setenv("COPILOT_MODEL", "gemini-test-model")
    assert build_diagnosis_agent().model == "gemini-test-model"
    monkeypatch.delenv("COPILOT_MODEL")
    assert build_diagnosis_agent().model == dx.DEFAULT_MODEL


def test_prompt_is_loaded_verbatim_without_templating():
    agent = build_diagnosis_agent(model=scripted(FINAL))
    prompt = load_prompt("diagnosis")
    assert callable(agent.instruction) and agent.instruction(None) == prompt
    assert '{status_code="STATUS_CODE_ERROR"}' in prompt                 # PromQL braces survive


def test_prompt_teaches_the_lessons_and_never_mentions_fault_switches():
    prompt = load_prompt("diagnosis")
    assert 'level="warn"' in prompt and "get_service_health" in prompt and "error_origins" in prompt
    assert "flag" not in prompt.lower()


def test_load_prompt_rejects_paths():
    with pytest.raises(ValueError):
        load_prompt("../secrets")


def test_tool_guard_blocks_unknown_tools_and_enforces_budget():
    guard = dx._tool_guard(max_tool_calls=2)
    ctx = SimpleNamespace(state={})
    assert guard(SimpleNamespace(name="set_flag"), {}, ctx)["error"]["type"] == "forbidden_tool"
    known = SimpleNamespace(name="get_traces")
    assert guard(known, {}, ctx) is None and guard(known, {}, ctx) is None
    assert guard(known, {}, ctx)["error"]["type"] == "budget_exhausted"


# --------------------------------------------------------------------------- #
# The ReAct loop with a scripted model
# --------------------------------------------------------------------------- #
def test_full_investigation_records_every_step_and_parses_the_answer():
    llm = scripted({"call": "get_service_health", "args": {"minutes": 15}},
                   {"call": "get_traces", "args": {"service": "checkout", "errors_only": True}},
                   FINAL)
    run = diagnose("checkout errors above 5%", agent=build_diagnosis_agent(model=llm))
    assert run.error is None
    assert run.diagnosis == Diagnosis.model_validate(FINAL)
    kinds = [s["type"] for s in run.steps]
    assert kinds == ["tool_call", "tool_result", "tool_call", "tool_result", "final"]
    assert run.steps[0] == {"type": "tool_call", "tool": "get_service_health", "args": {"minutes": 15}}
    assert run.steps[1]["result"]["status"] == "error"                   # backends are offline in unit tests
    assert run.tool_calls == 2 and run.tokens["total"] == 330 and run.latency_seconds >= 0
    json.dumps(run.to_dict())                                             # transcript is serialisable


def test_answer_via_set_model_response_like_ai_studio_gemini():
    llm = ScriptedLlm(native_schema=False, requests=[], script=[
        {"call": "get_service_health", "args": {"minutes": 15}},
        {"call": "set_model_response", "args": FINAL},
    ])
    run = diagnose("checkout errors", agent=build_diagnosis_agent(model=llm, max_tool_calls=1))
    assert "set_model_response" in [d.name for d in llm.requests[0].config.tools[0].function_declarations]
    assert run.error is None and run.diagnosis == Diagnosis.model_validate(FINAL)
    assert run.tool_calls == 1                                  # the answer tool is not an investigation step
    assert [s["type"] for s in run.steps] == ["tool_call", "tool_result", "answer_submitted", "final"]


def test_tool_results_reach_the_model():
    llm = scripted({"call": "get_service_health", "args": {"minutes": 1}}, FINAL)   # invalid -> error result
    diagnose("alert", agent=build_diagnosis_agent(model=llm))
    responses = [p.function_response for c in llm.requests[-1].contents for p in c.parts or []
                 if p.function_response]
    assert responses and responses[0].response["error"]["type"] == "invalid_input"


def test_budget_stops_a_looping_model():
    loop = {"call": "get_traces", "args": {"service": "checkout"}}
    llm = scripted(loop, loop, loop, FINAL)
    run = diagnose("alert", agent=build_diagnosis_agent(model=llm, max_tool_calls=2))
    results = [s["result"] for s in run.steps if s["type"] == "tool_result"]
    assert results[-1]["error"]["type"] == "budget_exhausted"
    assert run.diagnosis is not None


def test_unknown_tool_requested_by_model_is_not_executed():
    llm = scripted({"call": "set_feature_flag", "args": {"name": "x"}}, FINAL)
    run = diagnose("alert", agent=build_diagnosis_agent(model=llm))
    assert all(s.get("tool") != "set_feature_flag" or s["type"] != "tool_result" or
               s["result"]["status"] == "error" for s in run.steps)


def test_no_incident_answer_is_valid():
    calm = dict(FINAL, incident=False, root_cause_service="none", failure_type="none", confidence="medium",
                affected_services=[], alternative_causes=[])
    run = diagnose("possible false alarm", agent=build_diagnosis_agent(model=scripted(calm)))
    assert run.diagnosis.incident is False and run.diagnosis.root_cause_service == "none"


def test_model_failure_keeps_the_transcript_and_reports_the_error():
    llm = scripted({"call": "get_service_health", "args": {"minutes": 15}}, RuntimeError("quota exceeded"))
    run = diagnose("alert", agent=build_diagnosis_agent(model=llm))
    assert run.diagnosis is None and "quota exceeded" in run.error
    assert run.steps[0]["type"] == "tool_call"


def test_invalid_final_answer_is_reported_not_raised():
    run = diagnose("alert", agent=build_diagnosis_agent(model=scripted("I think it is payment.")))
    assert run.diagnosis is None and run.error


# --------------------------------------------------------------------------- #
# Parsing and printing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    json.dumps(FINAL),
    "```json\n" + json.dumps(FINAL) + "\n```",
    "Here is my answer: " + json.dumps(FINAL) + " Done.",
    FINAL,
])
def test_parse_diagnosis_accepts_common_shapes(text):
    assert parse_diagnosis(text).root_cause_service == "payment"


@pytest.mark.parametrize("bad", [None, "", "no json here", "{broken", {"incident": True}, json.dumps(
    dict(FINAL, failure_type="cosmic rays"))])
def test_parse_diagnosis_rejects_bad_answers(bad):
    assert parse_diagnosis(bad) is None


def test_transcript_printing():
    assert format_step({"type": "tool_call", "tool": "get_traces", "args": {"service": "checkout"}}) == \
        "ACT     get_traces(service='checkout')"
    out = format_step({"type": "tool_result", "tool": "get_traces",
                       "result": {"status": "ok", "size_bytes": 2048, "error_origins": {"payment :: charge": 6}}})
    assert "OBSERVE get_traces -> ok (2.0 KB)" in out and "payment :: charge" in out
    assert "root cause:   payment" in format_step({"type": "final", "diagnosis": FINAL})
