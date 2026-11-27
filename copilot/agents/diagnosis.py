"""The single diagnosis agent (Week 2): one Google ADK LlmAgent with read-only tools.

    agent = build_diagnosis_agent()                 # Gemini, from COPILOT_MODEL
    agent = build_diagnosis_agent(knowledge=True)   # + runbook / postmortem search (Week 3)
    run = diagnose("checkout error ratio above 5% since 11:53 UTC")
    run.diagnosis.root_cause_service                # "payment"
    run.steps                                       # every tool call and result summary

How it works
------------
ADK turns each tool function into a FunctionTool: the name, type hints and
docstring become the tool description Gemini sees. Gemini replies with
"call get_traces(service='checkout', errors_only=True)"; ADK runs the Python
function and sends the result back; this repeats (the ReAct loop) until
Gemini returns the final answer, which ADK validates against ``Diagnosis``.

Safety
------
* Only the read-only tools in ``copilot.tools.TOOLS`` are attached (plus
  ``copilot.tools.KB_TOOLS`` with ``knowledge=True``). There is no flag, shell,
  HTTP or write tool, so the agent cannot see or change the fault switches.
* ``before_tool_callback`` enforces an allow-list (defence in depth) and a
  tool-call budget, so a confused model cannot loop forever or run up cost.
  ADK's internal ``set_model_response`` (how the final JSON is handed in when
  tools and an output schema cannot be combined natively) is let through.
* The instruction is passed as a callable so ADK does not try to substitute
  ``{...}`` placeholders inside the PromQL examples of the prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from copilot.prompts import load_prompt
from copilot.tools import KB_TOOLS, TOOLS

__all__ = ["Diagnosis", "DiagnosisRun", "Evidence", "build_diagnosis_agent", "diagnose", "run_diagnosis"]

DEFAULT_MODEL = "gemini-2.5-flash"
MAX_TOOL_CALLS = 15
APP_NAME = "incident-copilot"
OUTPUT_KEY = "diagnosis"
STATE_TOOL_CALLS = "tool_calls_used"
ALLOWED_TOOL_NAMES = frozenset(t.__name__ for t in TOOLS)
KNOWLEDGE_TOOL_NAMES = frozenset(t.__name__ for t in KB_TOOLS)
# ADK adds this internal tool when the model cannot combine tools with an output
# schema natively (e.g. Gemini through an AI Studio key): the model "calls" it to
# hand in the final JSON answer. It reads and changes nothing.
ANSWER_TOOL = "set_model_response"

FailureType = Literal["errors", "latency", "resource", "queue_lag", "traffic_spike", "unavailable", "none",
                      "unknown"]


# --------------------------------------------------------------------------- #
# Structured answer
# --------------------------------------------------------------------------- #
class Evidence(BaseModel):
    kind: Literal["metric", "trace", "log"] = Field(description="Which signal this evidence comes from.")
    detail: str = Field(description="The concrete value, trace ID or log pattern, quoted from a tool result.")


class Diagnosis(BaseModel):
    """The agent's final answer. Scored automatically in the Week 5 evaluation."""

    incident: bool = Field(description="False if nothing is actually wrong (false alarm).")
    root_cause_service: str = Field(description='Service that started the problem, or "none".')
    failure_type: FailureType
    confidence: Literal["high", "medium", "low"]
    summary: str = Field(description="2-3 sentences for the on-call engineer.")
    affected_services: list[str] = Field(default_factory=list, description="Services showing symptoms.")
    evidence: list[Evidence] = Field(default_factory=list)
    alternative_causes: list[str] = Field(default_factory=list, description="Up to 2 other suspects.")
    suggested_next_checks: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list, description=(
        "Runbooks or postmortems that informed the conclusion (their `source`, e.g. kb/runbooks/payment.md). "
        "Empty if none were used."))


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
def build_diagnosis_agent(model: str | BaseLlm | None = None, max_tool_calls: int = MAX_TOOL_CALLS, *,
                          knowledge: bool = False) -> LlmAgent:
    """Create the diagnosis agent. ``model`` defaults to $COPILOT_MODEL or gemini-2.5-flash.

    ``knowledge=True`` adds ``search_runbooks`` and ``search_postmortems`` and the prompt section that
    explains them (copilot/prompts/diagnosis_knowledge.md). The default stays the Week-2 agent, so the
    "single agent without RAG" baseline can still be measured.
    """
    tools = list(TOOLS) + (list(KB_TOOLS) if knowledge else [])
    prompt = load_prompt("diagnosis")
    if knowledge:
        prompt += "\n\n" + load_prompt("diagnosis_knowledge")
    return LlmAgent(
        name="diagnosis",
        description="Finds the root cause of an incident from metrics, traces and logs (read-only).",
        model=model or os.environ.get("COPILOT_MODEL", DEFAULT_MODEL),
        instruction=lambda _ctx: prompt,
        tools=tools,
        output_schema=Diagnosis,
        output_key=OUTPUT_KEY,
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
        before_tool_callback=_tool_guard(max_tool_calls, frozenset(t.__name__ for t in tools)),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def _tool_guard(max_tool_calls: int, allowed: frozenset[str] = ALLOWED_TOOL_NAMES):
    """Allow-list + budget. Returning a dict skips the real tool and hands that dict to the model."""

    def guard(tool, args: dict[str, Any], tool_context) -> dict[str, Any] | None:
        if tool.name == ANSWER_TOOL:
            return None
        if tool.name not in allowed:
            return {"status": "error", "error": {"type": "forbidden_tool",
                                                 "message": f"Tool {tool.name!r} is not available."}}
        used = int(tool_context.state.get(STATE_TOOL_CALLS, 0)) + 1
        tool_context.state[STATE_TOOL_CALLS] = used
        if used > max_tool_calls:
            return {"status": "error", "error": {
                "type": "budget_exhausted",
                "message": f"Tool-call budget of {max_tool_calls} reached. Give your final answer now "
                           "with the evidence you already have."}}
        return None

    return guard


# --------------------------------------------------------------------------- #
# Running one investigation
# --------------------------------------------------------------------------- #
@dataclass
class DiagnosisRun:
    """Everything one investigation produced: the answer plus how it got there."""

    alert: str
    model: str
    diagnosis: Diagnosis | None = None
    raw_answer: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "output": 0, "total": 0})
    latency_seconds: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert": self.alert, "model": self.model,
            "diagnosis": self.diagnosis.model_dump() if self.diagnosis else None,
            "raw_answer": self.raw_answer, "steps": self.steps, "tool_calls": self.tool_calls,
            "tokens": self.tokens, "latency_seconds": self.latency_seconds, "error": self.error,
        }


async def run_diagnosis(alert: str, *, agent: LlmAgent | None = None, on_step=None) -> DiagnosisRun:
    """Investigate one alert. ``on_step(step_dict)`` is called live for each step (for printing)."""
    agent = agent or build_diagnosis_agent()
    model_name = agent.model if isinstance(agent.model, str) else getattr(agent.model, "model", "custom")
    run = DiagnosisRun(alert=alert, model=model_name)
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id="oncall",
                                                           session_id=f"inc-{uuid.uuid4().hex[:12]}")
    message = types.Content(role="user", parts=[types.Part(text=alert)])
    texts: list[str] = []

    def emit(step: dict[str, Any]) -> None:
        run.steps.append(step)
        if on_step:
            on_step(step)

    started = time.monotonic()
    try:
        async for event in runner.run_async(user_id="oncall", session_id=session.id, new_message=message):
            _add_usage(run, event)
            if event.error_code or event.error_message:
                emit({"type": "model_error", "code": event.error_code, "message": event.error_message})
            for part in (event.content.parts if event.content and event.content.parts else []):
                if part.function_call and part.function_call.name == ANSWER_TOOL:
                    emit({"type": "answer_submitted"})
                elif part.function_response and part.function_response.name == ANSWER_TOOL:
                    continue
                elif part.function_call:
                    run.tool_calls += 1
                    emit({"type": "tool_call", "tool": part.function_call.name,
                          "args": dict(part.function_call.args or {})})
                elif part.function_response:
                    emit({"type": "tool_result", "tool": part.function_response.name,
                          "result": summarise_result(part.function_response.response)})
                elif part.text and part.thought:
                    emit({"type": "thought", "text": part.text.strip()})
                elif part.text and part.text.strip():
                    texts.append(part.text)
                    if not event.is_final_response():
                        emit({"type": "reasoning", "text": part.text.strip()})
    except Exception as exc:  # network / quota / schema errors must not lose the transcript
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.latency_seconds = round(time.monotonic() - started, 2)

    run.raw_answer = texts[-1] if texts else ""
    stored = None
    try:
        final_session = await runner.session_service.get_session(app_name=APP_NAME, user_id="oncall",
                                                                 session_id=session.id)
        stored = final_session.state.get(OUTPUT_KEY) if final_session else None
    except Exception:
        stored = None
    run.diagnosis = parse_diagnosis(stored if stored is not None else run.raw_answer)
    if run.diagnosis is None and run.error is None:
        run.error = "The agent did not return a valid diagnosis."
    if run.diagnosis is not None:
        emit({"type": "final", "diagnosis": run.diagnosis.model_dump()})
    return run


def diagnose(alert: str, *, agent: LlmAgent | None = None, on_step=None) -> DiagnosisRun:
    """Synchronous wrapper around :func:`run_diagnosis` for scripts and the watcher."""
    return asyncio.run(run_diagnosis(alert, agent=agent, on_step=on_step))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_diagnosis(value: Any) -> Diagnosis | None:
    """Accept a dict, a JSON string, or JSON inside a ```json fence / surrounding prose."""
    if value is None or value == "":
        return None
    if isinstance(value, Diagnosis):
        return value
    if isinstance(value, str):
        text = value.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        elif not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                return None
            text = text[start:end + 1]
        try:
            value = json.loads(text)
        except ValueError:
            return None
    try:
        return Diagnosis.model_validate(value)
    except ValidationError:
        return None


def summarise_result(response: Any) -> dict[str, Any]:
    """A short view of a tool result for the transcript (the model saw the full result)."""
    if not isinstance(response, dict):
        return {"value": str(response)[:200]}
    out: dict[str, Any] = {"status": response.get("status"), "size_bytes": len(json.dumps(response, default=str))}
    if response.get("status") == "error":
        out["error"] = response.get("error")
    for key in ("services_with_observations", "total_matches", "series_count"):
        if key in response:
            out[key] = response[key]
    summary = response.get("summary")
    if isinstance(summary, dict):
        out["error_origins"] = summary.get("error_origins")
        out["trace_count"] = summary.get("trace_count")
    if isinstance(response.get("patterns"), list):
        out["top_patterns"] = [f"{p.get('service')}/{p.get('level')}: {str(p.get('pattern'))[:90]} x{p.get('count')}"
                               for p in response["patterns"][:3]]
    if isinstance(response.get("services"), list):
        out["top_services"] = [f"{s['service']}: {'; '.join(s['observations'])}"
                               for s in response["services"] if s.get("observations")][:4]
    if isinstance(response.get("results"), list):          # knowledge-base search
        out["retrieval"] = response.get("retrieval")
        out["top_results"] = [str(r.get("source")) for r in response["results"][:4] if isinstance(r, dict)]
    if isinstance(response.get("operations"), list):
        out["top_operations"] = [f"{o['operation']}: {'; '.join(o['observations']) or 'normal'}"
                                 for o in response["operations"][:3]]
    return out


def _add_usage(run: DiagnosisRun, event) -> None:
    usage = getattr(event, "usage_metadata", None)
    if usage is None or getattr(event, "partial", False):
        return
    run.tokens["prompt"] += usage.prompt_token_count or 0
    run.tokens["output"] += (usage.candidates_token_count or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
    run.tokens["total"] += usage.total_token_count or 0
