"""Draft postmortem from an incident record (Week 4).

Written from the record's structured data, not by an LLM: every line traces back to an
agent output, a timestamp or the human decision, so the draft cannot invent facts. Parts
that need human judgement (impact, lessons) are marked TODO.

The draft goes to ``runs/postmortems/`` (git-ignored), **not** into ``kb/``. A person reviews
it and moves it to ``kb/postmortems/`` before the next ingestion. Two reasons: postmortems
are reviewed documents, and automatic ingestion would let evaluation runs feed their own
answers back into the knowledge base (DECISIONS.md D-508).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from copilot.state import IncidentRecord, result_message
from copilot.tools import filters

__all__ = ["draft_postmortem", "write_postmortem"]

ROOT = Path(__file__).resolve().parents[1]


def postmortem_dir() -> Path:
    return Path(os.environ.get("COPILOT_POSTMORTEM_DIR", "").strip() or ROOT / "runs" / "postmortems")


def draft_postmortem(record: IncidentRecord) -> str:
    d = record.diagnosis or {}
    proposal = record.proposal or {}
    decision = record.decision or {}
    result = record.action_result or {}
    services = sorted({*(d.get("affected_services") or []), d.get("root_cause_service") or ""} - {"", "none"})
    started = _parse(record.created_at)
    fixed = _parse(next((t["at"] for t in reversed(record.timeline) if t["event"] == "action_result"), record.updated_at))
    minutes = max(0, int((fixed - started).total_seconds() // 60)) if started and fixed else 0
    args = proposal.get("args") or {}

    lines = [
        "---",
        f"incident_id: {record.id}",
        f"date: {record.created_at[:10]}",
        "severity: TODO",
        f"services: [{', '.join(services) or 'unknown'}]",
        f"root_cause_service: {d.get('root_cause_service') or 'unknown'}",
        f"failure_type: {d.get('failure_type') or 'unknown'}",
        f"duration_minutes: {minutes}",
        "status: draft",
        "---",
        f"# Postmortem (draft): {d.get('root_cause_service', 'unknown')} {d.get('failure_type', '')} incident",
        "",
        "## Summary",
        d.get("summary") or "TODO",
        "",
        "## Impact",
        f"TODO (affected services: {', '.join(d.get('affected_services') or []) or 'unknown'}).",
        "",
        "## Timeline (UTC)",
        *[f"- {t['at'][11:19]} {t['event']}{': ' + t['detail'] if t.get('detail') else ''}" for t in record.timeline],
        "",
        "## Root cause",
        f"{d.get('root_cause_service', 'unknown')} ({d.get('failure_type', 'unknown')}, "
        f"confidence {d.get('confidence', 'unknown')}). {d.get('summary', '')}",
        "",
        "## Detection and diagnosis",
        f"Alert: {record.alert}",
        "",
        "Evidence:",
        *[f"- [{e.get('kind')}] {e.get('detail')}" for e in d.get("evidence") or []],
        "",
        f"Review: {len(record.reviews)} round(s), final verdict "
        f"{record.reviews[-1]['verdict'] if record.reviews else 'none'}.",
        *([f"References: {', '.join(d['references'])}"] if d.get("references") else []),
        "",
        "## Resolution",
        f"Proposed by the copilot: {proposal.get('tool', 'none')}({', '.join(f'{k}={v!r}' for k, v in args.items())})",
        f"Decision: {'approved' if decision.get('approved') else 'rejected'} by {decision.get('by', 'unknown')}"
        f"{' (' + decision['note'] + ')' if decision.get('note') else ''}.",
        f"Result: {result_message(result) or 'unknown'}",
        *([f"Verification: {record.verification.get('message')}"] if record.verification else []),
        "",
        "## Lessons and action items",
        "- TODO",
        "",
    ]
    # Defence in depth: nothing that names the fault-injection mechanism may reach the knowledge base.
    return "\n".join(filters.REDACTED if filters.mentions_flags(line) else line for line in lines)


def write_postmortem(record: IncidentRecord, folder: Path | None = None) -> str:
    folder = folder or postmortem_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{record.created_at[:10]}-{record.id}.md"
    path.write_text(draft_postmortem(record), encoding="utf-8")
    return str(path)


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
