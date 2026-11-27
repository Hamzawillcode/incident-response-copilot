"""The incident team (Week 4): five specialist ADK agents, each with one job.

    team = build_team()                       # Gemini for every agent ($COPILOT_MODEL)
    team.triage, team.knowledge, team.diagnosis, team.reviewer, team.action

| Agent     | Job                              | Tools                                  | Output          |
|-----------|----------------------------------|----------------------------------------|-----------------|
| triage    | What is broken, how badly?       | the 5 telemetry tools (budget 4)       | TriageReport    |
| knowledge | Have we seen this before?        | none: retrieval runs in code (RAG)     | KnowledgeReport |
| diagnosis | Why is it broken? (ReAct)        | all 7 read-only tools (10)             | Diagnosis       |
| reviewer  | Is the evidence solid?           | none                                   | Review          |
| action    | What should we do?               | rollback_config / restart_service      | (one tool call) |
|           |                                  | (human approval) / escalate_to_human   |                 |

Only the action agent can change anything, and only through ADK tool confirmation. The
order in which they run, and when the run stops for a human, is decided by the
coordinator (``copilot.agents.coordinator``), in code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types
from pydantic import BaseModel, Field

from copilot.actions import ACTION_TOOLS
from copilot.agents.diagnosis import build_diagnosis_agent
from copilot.agents.llm import pace_model_calls, team_model
from copilot.agents.runtime import tool_guard
from copilot.prompts import load_prompt
from copilot.tools import TOOLS

__all__ = ["AGENT_NAMES", "KnowledgeReport", "MAX_TOOL_CALLS", "PastIncident", "Review", "RunbookNote", "Symptom",
           "Team", "TriageReport", "build_team"]

AGENT_NAMES = ("triage", "knowledge", "diagnosis", "reviewer", "action")
# Version 2 (after the first live runs, D-514): smaller budgets, knowledge retrieval moved into code.
MAX_TOOL_CALLS = {"triage": 4, "knowledge": 0, "diagnosis": 10, "reviewer": 0, "action": 1}


# --------------------------------------------------------------------------- #
# Structured outputs
# --------------------------------------------------------------------------- #
class Symptom(BaseModel):
    service: str
    signal: Literal["errors", "latency", "traffic", "resource", "queue", "logs"]
    detail: str = Field(description="The value or pattern seen, quoted from a tool result.")


class TriageReport(BaseModel):
    incident_likely: bool = Field(description="False only if nothing beyond normal background noise is visible.")
    summary: str = Field(description="Two sentences for the on-call engineer.")
    symptoms: list[Symptom] = Field(default_factory=list)
    suspect_services: list[str] = Field(default_factory=list, description="Up to 3, most likely first.")
    affected_services: list[str] = Field(default_factory=list)
    started_at: str = Field(default="", description="When symptoms started (UTC), or empty.")
    search_query: str = Field(default="", description="Plain-words symptom description for the knowledge base.")


class RunbookNote(BaseModel):
    source: str
    why_relevant: str
    key_steps: list[str] = Field(default_factory=list)


class PastIncident(BaseModel):
    source: str
    root_cause_service: str
    failure_type: str
    similarity: str
    differences: str = ""


class KnowledgeReport(BaseModel):
    runbooks: list[RunbookNote] = Field(default_factory=list)
    past_incidents: list[PastIncident] = Field(default_factory=list)
    suggested_checks: list[str] = Field(default_factory=list)
    remediation_hints: list[str] = Field(default_factory=list)
    note: str = ""


class Review(BaseModel):
    verdict: Literal["approve", "revise"]
    summary: str = Field(description="One sentence.")
    issues: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, description="For the diagnosis agent to answer with tools.")


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Team:
    triage: LlmAgent
    knowledge: LlmAgent
    diagnosis: LlmAgent
    reviewer: LlmAgent
    action: LlmAgent

    def agents(self) -> dict[str, LlmAgent]:
        return {name: getattr(self, name) for name in AGENT_NAMES}


def build_team(model: str | BaseLlm | None = None, models: dict[str, Any] | None = None) -> Team:
    """Build the five agents. ``models`` overrides the model per agent name (tests use scripted models)."""

    def pick(name: str):  # Gemini with retries on 429/5xx, or a test's scripted model
        return team_model((models or {}).get(name) or model)

    return Team(
        triage=_specialist("triage", "Fast, broad first look: what is broken and where to start (read-only).",
                           "triage", TOOLS, pick("triage"), TriageReport, "triage_report"),
        knowledge=_specialist("knowledge", "Summarises the runbooks and past incidents retrieved for the symptoms.",
                              "knowledge", (), pick("knowledge"), KnowledgeReport, "knowledge_report"),
        diagnosis=build_diagnosis_agent(pick("diagnosis"), MAX_TOOL_CALLS["diagnosis"], knowledge=True, team=True,
                                        before_model_callback=pace_model_calls),
        reviewer=_specialist("reviewer", "Checks that the diagnosis is grounded in evidence (no tools).",
                             "reviewer", (), pick("reviewer"), Review, "review"),
        action=_specialist("action", "Proposes one remediation; write tools need human approval.",
                           "action", ACTION_TOOLS, pick("action"), None, None,
                           after_tool_callback=_end_after_action),
    )


def _end_after_action(tool, args, tool_context, tool_response):
    """The action's tool result *is* the answer: skip the extra model call that would summarise it."""
    tool_context.actions.skip_summarization = True
    return None


def _specialist(name: str, description: str, prompt_name: str, tools, model, output_schema, output_key, *,
                after_tool_callback=None) -> LlmAgent:
    prompt = load_prompt(prompt_name)
    allowed = frozenset(_tool_name(t) for t in tools)
    return LlmAgent(
        name=name,
        description=description,
        model=model,
        instruction=lambda _ctx: prompt,          # used verbatim: no {placeholder} templating
        tools=list(tools),
        output_schema=output_schema,
        output_key=output_key,
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
        before_tool_callback=tool_guard(allowed, MAX_TOOL_CALLS[name]) if tools else None,
        before_model_callback=pace_model_calls,      # free-tier pacing (COPILOT_LLM_RPM), see agents/llm.py
        after_tool_callback=after_tool_callback,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def _tool_name(tool) -> str:
    return getattr(tool, "name", None) or tool.__name__
