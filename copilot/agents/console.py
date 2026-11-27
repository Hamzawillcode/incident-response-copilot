"""Human-readable printing of an investigation, shared by scripts and the watcher."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNS_DIR = Path(__file__).resolve().parent.parent.parent / "runs"

_LABEL = {"tool_call": "ACT    ", "tool_result": "OBSERVE", "reasoning": "THINK  ", "thought": "THINK  ",
          "model_error": "ERROR  "}


def format_step(step: dict[str, Any]) -> str:
    kind = step.get("type")
    if kind == "tool_call":
        args = ", ".join(f"{k}={v!r}" for k, v in step.get("args", {}).items())
        return f"{_LABEL[kind]} {step['tool']}({args})"
    if kind == "tool_result":
        res = dict(step.get("result") or {})
        head = f"{_LABEL[kind]} {step['tool']} -> {res.pop('status', '?')} ({res.pop('size_bytes', 0) / 1024:.1f} KB)"
        lines = [head] + [f"          {k}: {_short(v)}" for k, v in res.items() if v not in (None, [], {})]
        return "\n".join(lines)
    if kind in ("reasoning", "thought"):
        return f"{_LABEL[kind]} {_short(step.get('text', ''), 400)}"
    if kind == "answer_submitted":
        return "SUBMIT  final answer"
    if kind == "model_error":
        return f"{_LABEL[kind]} {step.get('code')}: {step.get('message')}"
    if kind == "final":
        return "ANSWER\n" + format_diagnosis(step["diagnosis"])
    return json.dumps(step, default=str)[:300]


def format_diagnosis(d: dict[str, Any]) -> str:
    lines = [
        f"  incident:     {d.get('incident')}",
        f"  root cause:   {d.get('root_cause_service')}  ({d.get('failure_type')}, confidence {d.get('confidence')})",
        f"  summary:      {d.get('summary')}",
        f"  affected:     {', '.join(d.get('affected_services') or []) or '-'}",
        "  evidence:",
        *[f"    - [{e.get('kind')}] {e.get('detail')}" for e in d.get("evidence") or []],
    ]
    if d.get("alternative_causes"):
        lines.append(f"  alternatives: {', '.join(d['alternative_causes'])}")
    if d.get("suggested_next_checks"):
        lines.append("  next checks:")
        lines += [f"    - {c}" for c in d["suggested_next_checks"]]
    if d.get("references"):
        lines.append(f"  references:   {', '.join(d['references'])}")
    return "\n".join(lines)


def print_step(step: dict[str, Any]) -> None:
    print(format_step(step), flush=True)


def save_run(run_dict: dict[str, Any], prefix: str = "diagnosis") -> Path:
    """Write the full run (alert, steps, answer, tokens, latency) to runs/<timestamp>-<prefix>.json."""
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS_DIR / f"{stamp}-{prefix}.json"
    path.write_text(json.dumps(run_dict, indent=2, default=str))
    return path


def _short(value: Any, limit: int = 220) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------- #
# The incident team (Week 4)
# --------------------------------------------------------------------------- #
def format_team_step(agent: str, step: dict[str, Any]) -> str:
    """One step of one agent, prefixed with the agent's name, e.g. ``[triage   ] ACT ...``."""
    kind = step.get("type")
    if kind == "approval_requested":
        args = ", ".join(f"{k}={v!r}" for k, v in (step.get("args") or {}).items())
        body = f"APPROVAL NEEDED  {step.get('tool')}({args})"
    elif kind == "awaiting_approval":
        body = f"PAUSED  {step.get('tool')} waits for a human decision"
    elif kind == "output":
        body = "RESULT\n" + format_output(agent, step.get("output") or {})
    elif kind == "answer":
        body = f"ANSWER  {_short(step.get('text', ''), 400)}"
    else:
        body = format_step(step)
    prefix = f"[{agent:<9}] "
    return "\n".join((prefix if i == 0 else " " * len(prefix)) + line for i, line in enumerate(body.split("\n")))


def print_team_step(agent: str, step: dict[str, Any]) -> None:
    print(format_team_step(agent, step), flush=True)


def format_output(agent: str, out: dict[str, Any]) -> str:
    if agent == "diagnosis":
        return format_diagnosis(out)
    if agent == "triage":
        lines = [f"  incident likely: {out.get('incident_likely')}",
                 f"  summary:         {out.get('summary')}",
                 f"  suspects:        {', '.join(out.get('suspect_services') or []) or '-'}",
                 *[f"    - {s.get('service')} [{s.get('signal')}] {s.get('detail')}" for s in out.get("symptoms") or []],
                 f"  search query:    {out.get('search_query')}"]
        return "\n".join(lines)
    if agent == "knowledge":
        lines = [*[f"  runbook:  {r.get('source')}  ({r.get('why_relevant')})" for r in out.get("runbooks") or []],
                 *[f"  past:     {p.get('source')}  root cause {p.get('root_cause_service')}"
                   for p in out.get("past_incidents") or []],
                 *[f"  hint:     {h}" for h in out.get("remediation_hints") or []]]
        if out.get("note"):
            lines.append(f"  note:     {out['note']}")
        return "\n".join(lines) or "  (nothing relevant found)"
    if agent == "reviewer":
        lines = [f"  verdict:  {out.get('verdict')}   {out.get('summary', '')}",
                 *[f"  issue:    {i}" for i in out.get("issues") or []],
                 *[f"  question: {q}" for q in out.get("questions") or []]]
        return "\n".join(lines)
    return json.dumps(out, indent=1, default=str)[:1500]


def format_incident(record: dict[str, Any]) -> str:
    """A short report of an incident record (``IncidentRecord.model_dump()``)."""
    d = record.get("diagnosis") or {}
    lines = [f"incident {record.get('id')}   status: {str(record.get('status')).upper()}",
             f"  alert:      {_short(record.get('alert', ''), 160)}"]
    if d:
        lines.append(f"  root cause: {d.get('root_cause_service')} ({d.get('failure_type')}, "
                     f"confidence {d.get('confidence')})")
        lines.append(f"  summary:    {_short(d.get('summary', ''), 300)}")
    for review in record.get("reviews") or []:
        g = review.get("grounding") or {}
        lines.append(f"  review {review.get('round')}:   {review.get('verdict')}   "
                     f"(evidence anchored: {g.get('evidence_anchored')}/{g.get('evidence_total')}"
                     f"{', HARD: ' + '; '.join(g.get('hard_failures')) if g.get('hard_failures') else ''})")
    proposal = record.get("proposal")
    if proposal:
        args = ", ".join(f"{k}={v!r}" for k, v in (proposal.get("args") or {}).items())
        lines.append(f"  proposal:   {proposal.get('tool')}({args})")
    if record.get("decision"):
        dec = record["decision"]
        lines.append(f"  decision:   {'approved' if dec.get('approved') else 'rejected'} by {dec.get('by')} "
                     f"at {dec.get('at')}")
    if record.get("action_result"):
        from copilot.state import result_message
        lines.append(f"  result:     {result_message(record['action_result'])}")
    if record.get("verification"):
        lines.append(f"  verified:   {record['verification'].get('message')}")
    if record.get("postmortem_path"):
        lines.append(f"  postmortem: {record['postmortem_path']}  (draft)")
    steps = record.get("steps") or []
    lines.append(f"  effort:     {len(steps)} agent steps, {sum(s.get('llm_calls', 0) for s in steps)} LLM calls, "
                 f"{sum(s.get('tool_calls', 0) for s in steps)} tool calls, "
                 f"{sum((s.get('tokens') or {}).get('total', 0) for s in steps)} tokens, "
                 f"{sum(s.get('latency_seconds', 0) for s in steps):.1f} s of agent time "
                 f"({sum(s.get('rate_limit_wait_seconds', 0) for s in steps):.0f} s waiting for the rate limit)")
    for error in record.get("errors") or []:
        lines.append(f"  error:      {_short(error, 200)}")
    return "\n".join(lines)
