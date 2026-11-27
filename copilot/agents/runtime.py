"""Running one agent step (Week 4): ADK events -> transcript, tokens, parsed output, paused approvals.

    run = await run_step(agent, message, session_service=svc, session_id="inc-...-triage",
                         schema=TriageReport)
    run.output            # the parsed structured answer (or None)
    run.tool_results      # every tool result in full (the Reviewer's grounding check reads them)
    run.pending_approval  # set when a write tool is waiting for a human (ADK tool confirmation)

Resuming after a human decision is the same call with ``approval_message(...)`` as the
message and the same ``session_id``: ADK finds the paused call in the stored session,
runs the tool if it was approved, and lets the agent finish.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.genai import types
from pydantic import BaseModel, ValidationError

from copilot.agents.diagnosis import summarise_result
from copilot.state import APP_NAME, USER_ID

__all__ = ["ANSWER_TOOL", "CONFIRMATION_TOOL", "StepRun", "approval_message", "parse_output", "run_step",
           "tool_guard"]

ANSWER_TOOL = "set_model_response"          # ADK's internal "hand in the JSON answer" tool
CONFIRMATION_TOOL = "adk_request_confirmation"  # ADK's "a human must confirm this call" event
STATE_TOOL_CALLS = "tool_calls_used"
STATE_COUNTED_IDS = "counted_call_ids"


@dataclass
class StepRun:
    """What one agent step produced."""

    agent: str
    session_id: str
    output: BaseModel | None = None
    raw_answer: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)   # {"tool", "args", "response"}
    tool_calls: int = 0
    llm_calls: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "output": 0, "total": 0})
    latency_seconds: float = 0.0
    error: str | None = None
    pending_approval: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        """Compact view for the incident record (no full tool results)."""
        return {"agent": self.agent, "session_id": self.session_id, "tool_calls": self.tool_calls,
                "llm_calls": self.llm_calls, "tokens": dict(self.tokens), "latency_seconds": self.latency_seconds, "error": self.error,
                "tools_used": [r["tool"] for r in self.tool_results]}


def tool_guard(allowed: frozenset[str], max_calls: int):
    """``before_tool_callback``: allow-list plus a call budget, counted once per function call.

    ADK runs this callback *before* its confirmation gate, both when a write tool first pauses
    and again when the approved call is resumed. Counting by function-call ID keeps the
    resumed call from being counted (and refused) a second time.
    """

    def guard(tool, args: dict[str, Any], tool_context) -> dict[str, Any] | None:
        if tool.name == ANSWER_TOOL:
            return None
        if tool.name not in allowed:
            return {"status": "error", "error": {"type": "forbidden_tool",
                                                 "message": f"Tool {tool.name!r} is not available."}}
        call_id = getattr(tool_context, "function_call_id", None) or ""
        counted = list(tool_context.state.get(STATE_COUNTED_IDS, []))
        if call_id and call_id in counted:
            return None
        used = int(tool_context.state.get(STATE_TOOL_CALLS, 0)) + 1
        if used > max_calls:
            return {"status": "error", "error": {
                "type": "budget_exhausted",
                "message": f"Tool-call budget of {max_calls} reached. Give your final answer now "
                           "with what you already have."}}
        tool_context.state[STATE_TOOL_CALLS] = used
        if call_id:
            tool_context.state[STATE_COUNTED_IDS] = counted + [call_id]
        return None

    return guard


def approval_message(confirmation_call_id: str, *, approved: bool, by: str, note: str = "",
                     incident_id: str = "") -> types.Content:
    """The user message that answers ADK's confirmation request (approve or reject)."""
    response = {"confirmed": bool(approved),
                "payload": {"by": by, "note": note, "incident_id": incident_id}}
    return types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=confirmation_call_id, name=CONFIRMATION_TOOL, response=response))])


async def run_step(agent: LlmAgent, message: str | types.Content, *, session_service, session_id: str,
                   schema: type[BaseModel] | None = None,
                   on_step: Callable[[str, dict[str, Any]], None] | None = None) -> StepRun:
    """Run ``agent`` on one message in session ``session_id`` (created if missing). Never raises."""
    run = StepRun(agent=agent.name, session_id=session_id)
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)
    content = message if isinstance(message, types.Content) else types.Content(
        role="user", parts=[types.Part(text=message)])
    texts: list[str] = []
    calls: dict[str, dict[str, Any]] = {}

    def emit(step: dict[str, Any]) -> None:
        run.steps.append(step)
        if on_step:
            on_step(agent.name, step)

    started = time.monotonic()
    try:
        session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        if session is None:
            await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        async for event in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=content):
            _add_usage(run, event)
            if event.error_code or event.error_message:
                emit({"type": "model_error", "code": event.error_code, "message": event.error_message})
            for part in (event.content.parts if event.content and event.content.parts else []):
                _handle_part(run, part, event, emit, texts, calls)
    except Exception as exc:  # quota, network, schema errors: keep the transcript, report the error
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.latency_seconds = round(time.monotonic() - started, 2)

    run.raw_answer = texts[-1] if texts else ""
    if schema is not None:
        stored = None
        try:
            final = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
            stored = final.state.get(agent.output_key) if (final and agent.output_key) else None
        except Exception:
            stored = None
        run.output = parse_output(schema, stored if stored is not None else run.raw_answer)
        if run.output is None and run.error is None and run.pending_approval is None:
            run.error = f"The {agent.name} agent did not return a valid {schema.__name__}."
        if run.output is not None:
            emit({"type": "output", "output": run.output.model_dump(mode="json")})
    elif run.raw_answer and run.pending_approval is None:
        emit({"type": "answer", "text": run.raw_answer.strip()[:1000]})
    return run


def _handle_part(run: StepRun, part: types.Part, event, emit, texts: list[str], calls: dict[str, Any]) -> None:
    call, response = part.function_call, part.function_response
    if call and call.name == ANSWER_TOOL:
        emit({"type": "answer_submitted"})
    elif call and call.name == CONFIRMATION_TOOL:
        args = dict(call.args or {})
        original = args.get("originalFunctionCall") or {}
        run.pending_approval = {
            "confirmation_call_id": call.id,
            "tool": original.get("name"),
            "args": dict(original.get("args") or {}),
            "original_call_id": original.get("id"),
            "hint": (args.get("toolConfirmation") or {}).get("hint", ""),
        }
        emit({"type": "approval_requested", "tool": original.get("name"), "args": dict(original.get("args") or {})})
    elif call:
        run.tool_calls += 1
        calls[call.id or f"call-{run.tool_calls}"] = {"tool": call.name, "args": dict(call.args or {})}
        emit({"type": "tool_call", "tool": call.name, "args": dict(call.args or {})})
    elif response and response.name in (ANSWER_TOOL, CONFIRMATION_TOOL):
        return
    elif response:
        payload = response.response if isinstance(response.response, dict) else {"value": response.response}
        waiting = getattr(event.actions, "requested_tool_confirmations", None) or {}
        if response.id in waiting or "requires confirmation" in str(payload.get("error", "")):
            # ADK's placeholder while the call waits for a human (for a single call ADK emits it
            # right after the confirmation request): not a real result, so it is neither shown
            # nor graded. Recognised by the event's own marker, so the order does not matter.
            emit({"type": "awaiting_approval", "tool": response.name})
            return
        made = calls.get(response.id or "", {"tool": response.name, "args": {}})
        run.tool_results.append({"tool": response.name, "args": made.get("args", {}), "response": payload})
        emit({"type": "tool_result", "tool": response.name, "result": summarise_result(payload)})
    elif part.text and part.thought:
        emit({"type": "thought", "text": part.text.strip()})
    elif part.text and part.text.strip():
        texts.append(part.text)
        if not event.is_final_response():
            emit({"type": "reasoning", "text": part.text.strip()})


def parse_output(schema: type[BaseModel], value: Any) -> BaseModel | None:
    """Accept a dict, a model, a JSON string, or JSON inside a ```json fence / surrounding prose."""
    if value is None or value == "":
        return None
    if isinstance(value, schema):
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
        return schema.model_validate(value)
    except ValidationError:
        return None


def _add_usage(run: StepRun, event) -> None:
    usage = getattr(event, "usage_metadata", None)
    if usage is None or getattr(event, "partial", False):
        return
    run.llm_calls += 1
    run.tokens["prompt"] += usage.prompt_token_count or 0
    run.tokens["output"] += (usage.candidates_token_count or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
    run.tokens["total"] += usage.total_token_count or 0
