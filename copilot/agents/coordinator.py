"""The incident coordinator (Week 4): runs the agent team in a fixed order and stops for a human.

    coordinator = IncidentCoordinator()                        # Gemini, Postgres sessions + records
    record = await coordinator.investigate(alert)              # ... status "awaiting_approval"
    record = await coordinator.decide(record.id, approve=True, by="hamza")   # "fix_applied"

The procedure (the guide's Figure 4, as code)::

    triage -> knowledge -> diagnosis <-> reviewer (at most 2 rounds) -> action -> HUMAN -> result

Why the coordinator is code and not an LLM
------------------------------------------
Incident response has a known procedure, and the guarantees matter more than flexibility:
review always happens before any action; the run always stops at the same place for a human;
every step has a budget; a failed step leaves a record instead of a half-finished conversation;
and every evaluation run has the same structure. The LLMs decide *within* steps (what to query,
what the evidence means, what to propose), where judgement is needed. ADK's LLM-driven transfer
(a coordinator LlmAgent with sub_agents) could skip the reviewer or act before review.

State and resumption
--------------------
Each step runs in its own ADK session (``<incident>-triage``, ``<incident>-diagnosis-1``, ...)
stored by the session service (Postgres by default), and every step's output is saved in the
incident record right away. When the action agent calls a write tool, ADK pauses the run; the
record stores the paused call. ``decide()`` (possibly in another process, hours later) answers
ADK's confirmation request in the same session, ADK runs or refuses the tool, and the record is
completed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from copilot.actions import ROLLBACK_SERVICES, WRITE_TOOL_NAMES
from copilot.agents.diagnosis import Diagnosis
from copilot.agents.llm import pacer
from copilot.agents.review import check_grounding
from copilot.agents.runtime import StepRun, approval_message, run_step
from copilot.agents.team import KnowledgeReport, Review, TriageReport, build_team
from copilot.agents.verify import verify_recovery
from copilot.postmortem import write_postmortem
from copilot.tools import search_postmortems, search_runbooks
from copilot.state import (
    IncidentRecord, IncidentStore, StateError, make_session_service, new_incident_id, now_iso, result_message,
)

__all__ = ["DailyQuotaExhausted", "IncidentCoordinator", "MAX_REVIEW_ROUNDS"]

MAX_REVIEW_ROUNDS = 2


class DailyQuotaExhausted(Exception):
    """The model's daily request quota is used up: retrying or continuing cannot help (D-517)."""


class IncidentCoordinator:
    """Runs one incident at a time through the team. See the module docstring."""

    def __init__(self, *, model=None, models: dict[str, Any] | None = None, session_service=None, store=None,
                 max_review_rounds: int = MAX_REVIEW_ROUNDS,
                 on_step: Callable[[str, dict[str, Any]], None] | None = None,
                 postmortem_dir=None):
        self.team = build_team(model, models)
        self.session_service = session_service if session_service is not None else make_session_service()
        self.store = store if store is not None else IncidentStore()
        self.max_review_rounds = max(1, int(max_review_rounds))
        self.on_step = on_step
        self.postmortem_dir = postmortem_dir
        self.tracer = trace.get_tracer("incident-copilot")

    async def close(self) -> None:
        close = getattr(self.session_service, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    # ------------------------------------------------------------------ #
    # Investigation: everything up to the human decision
    # ------------------------------------------------------------------ #
    async def investigate(self, alert: str, *, incident_id: str | None = None) -> IncidentRecord:
        """Run the team on an alert. Ends in awaiting_approval, no_incident, needs_human or failed."""
        record = IncidentRecord(id=incident_id or new_incident_id(), alert=alert)
        record.note("alert", alert[:300])
        self.store.create(record)
        with self.tracer.start_as_current_span("incident", attributes={"incident.id": record.id}) as span:
            try:
                await self._investigate(record)
            except DailyQuotaExhausted as exc:   # stop at once: the next steps would wait and fail too
                record.status = "failed"
                record.errors.append(f"coordinator: {exc}")
                record.note("failed", "daily model quota used up")
            except Exception as exc:  # a bug or an outage must leave a record, not just a stack trace
                record.status = "failed"
                record.errors.append(f"coordinator: {type(exc).__name__}: {exc}")
                record.note("failed", f"{type(exc).__name__}")
            span.set_attribute("incident.status", record.status)
            self.store.save(record)
        return record

    async def _investigate(self, record: IncidentRecord) -> None:
        corpus: list[dict[str, Any]] = []          # every tool result, for the grounding check

        # 1. Triage: a fast, broad first look.
        triage = await self._step(record, "triage", _message(record.alert, [], "Do the first look."),
                                  TriageReport, "triage")
        corpus += triage.tool_results
        if triage.output is not None:
            record.triage = triage.output.model_dump(mode="json")
            record.note("triage", f"suspects: {', '.join(triage.output.suspect_services) or 'none'}")
        self.store.save(record)

        # 2. Knowledge: retrieval in code (deterministic, no model turn), then one model call that
        #    reads the retrieved documents and keeps what fits. Never blocks the investigation.
        query = (record.triage or {}).get("search_query") or record.alert
        runbooks = await asyncio.to_thread(search_runbooks, query[:500], 3)
        postmortems = await asyncio.to_thread(search_postmortems, query[:500], 2)
        record.retrieval = {"query": query, "retrieval": runbooks.get("retrieval"),
                            "runbooks": [r.get("source") for r in runbooks.get("results", [])],
                            "postmortems": [r.get("source") for r in postmortems.get("results", [])],
                            "errors": [r["error"] for r in (runbooks, postmortems) if r.get("status") == "error"]}
        knowledge = await self._step(
            record, "knowledge",
            _message(record.alert, [("Triage report", record.triage),
                                    ("Retrieved runbooks", _retrieved(runbooks)),
                                    ("Retrieved postmortems", _retrieved(postmortems))],
                     "Summarise the knowledge that fits these symptoms."),
            KnowledgeReport, "knowledge")
        if knowledge.output is not None:
            record.knowledge = knowledge.output.model_dump(mode="json")
            record.note("knowledge", f"{len(knowledge.output.runbooks)} runbook(s), "
                                     f"{len(knowledge.output.past_incidents)} past incident(s)")
        self.store.save(record)

        # 3. Diagnosis <-> review (self-reflection), at most max_review_rounds rounds.
        diagnosis: Diagnosis | None = None
        verdict = "revise"
        feedback: dict[str, Any] | None = None
        # When triage saw nothing beyond noise, say so: proving a negative should be a quick check,
        # not a full investigation (run 6: 13 model calls and 117k tokens for a false alarm, D-517).
        likely = (record.triage or {}).get("incident_likely", True)
        task = ("Investigate and give your diagnosis." if likely else
                "The triage agent found no sign of an incident. Confirm or refute that quickly: check the alerted "
                "service's metrics, error traces and warn logs in one turn, then answer.")
        for round_ in range(1, self.max_review_rounds + 1):
            sections = [("Triage report", record.triage), ("Knowledge", record.knowledge)]
            if feedback is not None:
                sections.append(("Reviewer feedback", feedback))
            run = await self._step(record, "diagnosis", _message(record.alert, sections, task),
                                   Diagnosis, f"diagnosis-{round_}")
            corpus += run.tool_results
            if run.output is None:
                break
            diagnosis = run.output
            record.diagnoses.append(diagnosis.model_dump(mode="json"))
            record.note("diagnosis", f"round {round_}: {diagnosis.root_cause_service} "
                                     f"({diagnosis.failure_type}, {diagnosis.confidence})")

            grounding = check_grounding(diagnosis, corpus)
            review_run = await self._step(
                record, "reviewer",
                _message(record.alert, [("Triage report", record.triage),
                                        ("Diagnosis", diagnosis.model_dump(mode="json")),
                                        ("Grounding check", grounding.to_dict())], "Review the diagnosis."),
                Review, f"review-{round_}")
            review = review_run.output
            approved = review is not None and review.verdict == "approve" and not grounding.hard_failures
            verdict = "approve" if approved else "revise"
            issues = grounding.hard_failures + (review.issues if review else
                                                ["The reviewer did not return a valid review."])
            record.reviews.append({"round": round_, "verdict": verdict, "grounding": grounding.to_dict(),
                                   "review": review.model_dump(mode="json") if review else None})
            record.note("review", f"round {round_}: {verdict}"
                                  f"{' (grounding: ' + '; '.join(grounding.hard_failures) + ')' if grounding.hard_failures else ''}")
            self.store.save(record)
            if approved:
                break
            feedback = {"your_previous_diagnosis": diagnosis.model_dump(mode="json"), "issues": issues,
                        "questions": review.questions if review else []}

        record.diagnosis = diagnosis.model_dump(mode="json") if diagnosis else None

        # 4. Act only on an approved diagnosis of a real incident.
        if diagnosis is None:
            record.status = "failed"
            record.note("failed", "no valid diagnosis")
            return
        if verdict != "approve":
            record.status = "needs_human"
            record.note("needs_human", f"diagnosis not approved after {len(record.reviews)} review round(s)")
            return
        if not diagnosis.incident:
            record.status = "no_incident"
            record.note("no_incident", diagnosis.summary[:200])
            return

        # 5. Action: one proposal. A write tool pauses here until a human decides.
        knowledge_hints = {k: (record.knowledge or {}).get(k) for k in ("remediation_hints", "runbooks")}
        action = await self._step(
            record, "action",
            _message(record.alert, [("Diagnosis (approved by the reviewer)", record.diagnosis),
                                    ("Knowledge", knowledge_hints),
                                    ("Services you can act on", sorted(ROLLBACK_SERVICES))],
                     "Propose one remediation."),
            None, "action")
        if action.pending_approval and action.pending_approval.get("tool") in WRITE_TOOL_NAMES:
            record.proposal = {**action.pending_approval, "session_id": action.session_id, "proposed_at": now_iso()}
            record.status = "awaiting_approval"
            args = ", ".join(f"{k}={v!r}" for k, v in record.proposal["args"].items() if k != "reason")
            record.note("proposal", f"{record.proposal['tool']}({args}) waits for approval")
            return
        escalation = next((r["response"] for r in action.tool_results if r["tool"] == "escalate_to_human"), None)
        record.action_result = escalation
        record.status = "needs_human"
        record.note("needs_human", "escalated by the action agent" if escalation else "no action proposed")

    # ------------------------------------------------------------------ #
    # The human decision
    # ------------------------------------------------------------------ #
    async def decide(self, incident_id: str, *, approve: bool, by: str, note: str = "",
                     verify_minutes: float = 0.0, verify_poll_seconds: float = 30.0) -> IncidentRecord:
        """Approve or reject the paused action, then resume the action agent. Applies exactly once."""
        current = self.store.get(incident_id)
        if current.status != "awaiting_approval" or not current.proposal:
            raise StateError(f"Incident {incident_id} is '{current.status}', not waiting for approval.")
        record = self.store.transition(incident_id, "awaiting_approval", "approved" if approve else "rejected")
        record.decision = {"approved": approve, "by": by, "note": note, "at": now_iso()}
        record.note("approved" if approve else "rejected", f"by {by}{': ' + note if note else ''}")
        self.store.save(record)

        proposal = record.proposal or {}
        with self.tracer.start_as_current_span("decision", attributes={"incident.id": record.id,
                                                                     "decision.approved": approve}) as span:
            try:
                run = await self._step(record, "action",
                                       approval_message(proposal["confirmation_call_id"], approved=approve, by=by,
                                                        note=note, incident_id=record.id),
                                       None, session_id=proposal["session_id"])
                result = next((r["response"] for r in run.tool_results if r["tool"] == proposal.get("tool")), None)
                record.action_result = result or {"status": "error", "error": {
                    "type": "no_result", "message": "The action agent did not report a tool result."}}
                record.note("action_result", result_message(record.action_result)[:200])
                if not approve:
                    record.status = "rejected"
                elif record.action_result.get("status") == "ok":
                    record.status = "fix_applied"
                    await self._after_fix(record, verify_minutes, verify_poll_seconds)
                else:
                    record.status = "failed"
            except Exception as exc:
                record.status = "failed" if approve else "rejected"
                record.errors.append(f"decision: {type(exc).__name__}: {exc}")
            span.set_attribute("incident.status", record.status)
            self.store.save(record)
        return record

    async def _after_fix(self, record: IncidentRecord, verify_minutes: float, poll_seconds: float) -> None:
        d = record.diagnosis or {}
        if verify_minutes > 0:
            services = [d.get("root_cause_service", "")] + list(d.get("affected_services") or [])
            record.verification = await verify_recovery(services, failure_type=d.get("failure_type", ""),
                                                        minutes=verify_minutes, poll_seconds=poll_seconds)
            record.note("verification", record.verification.get("message", ""))
            if record.verification.get("recovered"):
                record.status = "resolved"
        record.postmortem_path = write_postmortem(record, self.postmortem_dir)
        record.note("postmortem_draft", record.postmortem_path)

    # ------------------------------------------------------------------ #
    # One step
    # ------------------------------------------------------------------ #
    async def _step(self, record: IncidentRecord, agent_name: str, message, schema, suffix: str | None = None, *,
                    session_id: str | None = None) -> StepRun:
        agent = self.team.agents()[agent_name]
        session_id = session_id or f"{record.id}-{suffix}"
        waited_before = pacer().waited_seconds
        with self.tracer.start_as_current_span(f"step {agent_name}", attributes={
                "incident.id": record.id, "copilot.agent": agent_name, "copilot.session_id": session_id}) as span:
            run = await run_step(agent, message, session_service=self.session_service, session_id=session_id,
                                 schema=schema, on_step=self.on_step)
            waited = round(pacer().waited_seconds - waited_before, 1)
            span.set_attribute("copilot.tool_calls", run.tool_calls)
            span.set_attribute("copilot.llm_calls", run.llm_calls)
            span.set_attribute("copilot.tokens.total", run.tokens["total"])
            span.set_attribute("copilot.rate_limit_wait_seconds", waited)
            if run.error:
                span.set_status(Status(StatusCode.ERROR, run.error[:200]))
        record.steps.append({**run.summary(), "rate_limit_wait_seconds": waited})
        if run.error:
            record.errors.append(f"{agent_name}: {run.error}")
            if "PerDay" in run.error:   # quota IDs such as GenerateRequestsPerDayPerProjectPerModel-FreeTier
                raise DailyQuotaExhausted(
                    f"The daily request quota of the model is used up (in the {agent_name} step). Set "
                    "COPILOT_MODEL to another model, or wait for the quota to reset.")
        return run


def _retrieved(result: dict[str, Any]) -> dict[str, Any]:
    """What the knowledge agent sees of a search: the documents, or why there are none."""
    if result.get("status") == "ok":
        return {"retrieval": result.get("retrieval"), "results": result.get("results", [])}
    return {"status": result.get("status"), "message": result.get("message")
            or (result.get("error") or {}).get("message", "nothing found")}


def _message(alert: str, sections: list[tuple[str, Any]], task: str) -> str:
    """The step's input: the alert, the earlier agents' outputs as JSON, and the task."""
    parts = [f"ALERT: {alert}"]
    for title, value in sections:
        if value:
            parts.append(f"## {title}\n{json.dumps(value, indent=1, ensure_ascii=False, default=str)}")
    parts.append(f"## Your task\n{task}")
    return "\n\n".join(parts)
