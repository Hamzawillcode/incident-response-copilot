"""Grounding check (Week 4): does the diagnosis cite things that really appeared in tool results?

The Reviewer agent judges reasoning; this code checks facts it cannot see. It compares the
diagnosis with the *full* tool results of the investigation (triage and every diagnosis round):

| Check               | Hard? | What fails it                                                   |
|---------------------|-------|-----------------------------------------------------------------|
| consistency         | yes   | incident=true with root cause "none", or the reverse            |
| trace_ids_exist     | yes   | a 32-hex trace ID in the evidence that no tool ever returned     |
| root_cause_seen     | yes   | a root-cause service that no tool result mentions               |
| evidence_count      | no    | fewer than 2 evidence items for an incident (1 for no incident)  |
| confidence_vs_kinds | no    | "high" without metric, trace and log evidence; "medium" with one |
| evidence_anchored   | no    | evidence items with no trace ID, number or quoted text found in results |

A hard failure forces the verdict to "revise", whatever the Reviewer agent says. The share of
anchored evidence items is a rough "evidence-grounded rate" for the Week-5 evaluation: it shows
that a cited value *occurs* in the results, not that it supports the conclusion.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from copilot.agents.diagnosis import Diagnosis

__all__ = ["Grounding", "check_grounding"]

_TRACE_ID = re.compile(r"\b[0-9a-f]{32}\b")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")
_QUOTED = re.compile(r"['\"`]([^'\"`]{8,})['\"`]")


@dataclass
class Grounding:
    checks: list[dict[str, Any]] = field(default_factory=list)   # {"check", "ok", "hard", "detail"}
    evidence_total: int = 0
    evidence_anchored: int = 0

    @property
    def hard_failures(self) -> list[str]:
        return [c["detail"] for c in self.checks if c["hard"] and not c["ok"]]

    @property
    def soft_issues(self) -> list[str]:
        return [c["detail"] for c in self.checks if not c["hard"] and not c["ok"]]

    @property
    def anchored_rate(self) -> float | None:
        return round(self.evidence_anchored / self.evidence_total, 2) if self.evidence_total else None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "hard_failures": self.hard_failures, "soft_issues": self.soft_issues,
                "anchored_rate": self.anchored_rate}


def check_grounding(diagnosis: Diagnosis, tool_results: list[dict[str, Any]]) -> Grounding:
    """Compare ``diagnosis`` with the tool results it was built from (``StepRun.tool_results``)."""
    corpus = "\n".join(json.dumps(r.get("response"), default=str, ensure_ascii=False) for r in tool_results)
    corpus_lower = corpus.lower()
    known_trace_ids = set(_TRACE_ID.findall(corpus_lower))
    g = Grounding(evidence_total=len(diagnosis.evidence))

    def check(name: str, ok: bool, hard: bool, detail: str) -> None:
        g.checks.append({"check": name, "ok": ok, "hard": hard, "detail": detail})

    # 1. Internal consistency of the answer.
    root = diagnosis.root_cause_service.strip().lower()
    if diagnosis.incident:
        ok = root not in ("", "none") and diagnosis.failure_type != "none"
        check("consistency", ok, True, "consistent" if ok else
              "incident=true needs a root-cause service and a failure type other than 'none'.")
    else:
        ok = root in ("", "none") and diagnosis.failure_type == "none"
        check("consistency", ok, True, "consistent" if ok else
              "incident=false needs root_cause_service 'none' and failure_type 'none'.")

    # 2. Trace IDs must exist in some tool result (an invented ID is a fabrication).
    cited = sorted({t for e in diagnosis.evidence for t in _TRACE_ID.findall(e.detail.lower())})
    invented = [t for t in cited if t not in known_trace_ids]
    check("trace_ids_exist", not invented, True,
          f"{len(cited)} cited trace ID(s) found in tool results" if not invented else
          f"trace ID(s) not found in any tool result: {', '.join(invented)}")

    # 3. The root-cause service must appear in the tool results.
    if diagnosis.incident and root not in ("", "none"):
        seen = root in corpus_lower
        check("root_cause_seen", seen, True, f"'{root}' appears in tool results" if seen else
              f"root-cause service '{root}' does not appear in any tool result.")

    # 4. Enough evidence.
    needed = 2 if diagnosis.incident else 1
    check("evidence_count", len(diagnosis.evidence) >= needed, False,
          f"{len(diagnosis.evidence)} evidence item(s); at least {needed} expected.")

    # 5. Confidence vs. the kinds of evidence (prompt rule: high = metrics, traces and logs agree).
    kinds = {e.kind for e in diagnosis.evidence}
    if diagnosis.confidence == "high":
        check("confidence_vs_kinds", len(kinds) >= 3, False,
              f"confidence high with evidence kinds {sorted(kinds)}; high needs metric, trace and log.")
    elif diagnosis.confidence == "medium":
        check("confidence_vs_kinds", len(kinds) >= 2, False,
              f"confidence medium with evidence kinds {sorted(kinds)}; medium needs two kinds.")

    # 6. Each evidence item should contain something that occurs in the results.
    for item in diagnosis.evidence:
        if _anchored(item.detail, corpus_lower, known_trace_ids):
            g.evidence_anchored += 1
    if g.evidence_total:
        check("evidence_anchored", g.evidence_anchored == g.evidence_total, False,
              f"{g.evidence_anchored} of {g.evidence_total} evidence items quote a trace ID, number or text "
              "found in the tool results.")
    return g


def _anchored(detail: str, corpus_lower: str, known_trace_ids: set[str]) -> bool:
    text = detail.lower()
    if any(t in known_trace_ids for t in _TRACE_ID.findall(text)):
        return True
    for quoted in _QUOTED.findall(detail):
        if quoted.lower().strip() in corpus_lower:
            return True
    for number in _NUMBER.findall(text):
        # Small integers ("5", "15") occur everywhere; require a decimal or at least 3 digits.
        if ("." in number or len(number) >= 3) and number in corpus_lower:
            return True
    return False
