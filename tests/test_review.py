"""Unit tests for the grounding check that backs the Reviewer agent."""

from __future__ import annotations

from copilot.agents.diagnosis import Diagnosis
from copilot.agents.review import check_grounding

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
RESULTS = [
    {"tool": "get_traces", "args": {}, "response": {
        "status": "ok", "traces": [{"trace_id": TRACE, "error_origins": ["payment :: charge"]}],
        "summary": {"error_origins": {"payment :: charge": 5}}}},
    {"tool": "get_service_health", "args": {}, "response": {
        "status": "ok", "services": [{"service": "payment", "error_pct": {"now": 48.3, "window_start": 0.0}}]}},
    {"tool": "search_logs", "args": {}, "response": {
        "status": "ok", "patterns": [{"service": "payment", "pattern": "Payment request failed", "count": 16}]}},
]


def diagnosis(**changes) -> Diagnosis:
    base = {
        "incident": True, "root_cause_service": "payment", "failure_type": "errors", "confidence": "high",
        "summary": "Payment charges fail.", "affected_services": ["checkout"],
        "evidence": [{"kind": "metric", "detail": "payment error_pct now 48.3%, window start 0%"},
                     {"kind": "trace", "detail": f"trace {TRACE} error origin payment :: charge"},
                     {"kind": "log", "detail": "payment warn 'Payment request failed' x16"}],
    }
    return Diagnosis.model_validate({**base, **changes})


def checks(g) -> dict:
    return {c["check"]: c["ok"] for c in g.checks}


def test_a_grounded_diagnosis_passes_every_check():
    g = check_grounding(diagnosis(), RESULTS)
    assert g.hard_failures == [] and g.soft_issues == []
    assert g.evidence_anchored == 3 and g.anchored_rate == 1.0


def test_an_invented_trace_id_is_a_hard_failure():
    fake = "deadbeef" * 4
    d = diagnosis(evidence=[{"kind": "trace", "detail": f"trace {fake} fails in payment"},
                            {"kind": "metric", "detail": "payment error_pct now 48.3%"}])
    g = check_grounding(d, RESULTS)
    assert checks(g)["trace_ids_exist"] is False and fake in g.hard_failures[0]


def test_a_root_cause_no_tool_mentioned_is_a_hard_failure():
    g = check_grounding(diagnosis(root_cause_service="payment-gateway"), RESULTS)
    assert checks(g)["root_cause_seen"] is False


def test_inconsistent_answers_are_hard_failures():
    assert checks(check_grounding(diagnosis(root_cause_service="none"), RESULTS))["consistency"] is False
    no_incident = diagnosis(incident=False, root_cause_service="payment", failure_type="none")
    assert checks(check_grounding(no_incident, RESULTS))["consistency"] is False


def test_a_clean_no_incident_answer_passes():
    d = diagnosis(incident=False, root_cause_service="none", failure_type="none", confidence="medium",
                  evidence=[{"kind": "metric", "detail": "payment error_pct now 48.3% is a test value"},
                            {"kind": "log", "detail": "no new patterns"}])
    g = check_grounding(d, RESULTS)
    assert g.hard_failures == [] and "root_cause_seen" not in checks(g)


def test_confidence_must_fit_the_kinds_of_evidence():
    d = diagnosis(evidence=[{"kind": "metric", "detail": "payment error_pct now 48.3%"},
                            {"kind": "metric", "detail": "checkout errors too"}])
    g = check_grounding(d, RESULTS)
    assert checks(g)["confidence_vs_kinds"] is False and g.hard_failures == []      # soft only


def test_too_little_evidence_is_a_soft_issue():
    g = check_grounding(diagnosis(evidence=[{"kind": "metric", "detail": "payment error_pct now 48.3%"}],
                                  confidence="low"), RESULTS)
    assert checks(g)["evidence_count"] is False and g.hard_failures == []


def test_unanchored_evidence_is_counted():
    d = diagnosis(evidence=[{"kind": "log", "detail": "payment seems unhappy"},
                            {"kind": "metric", "detail": "error_pct now 48.3%"}], confidence="medium")
    g = check_grounding(d, RESULTS)
    assert g.evidence_anchored == 1 and g.anchored_rate == 0.5 and checks(g)["evidence_anchored"] is False


def test_small_numbers_do_not_count_as_anchors():
    """'5' or '15' occur in almost any result; they must not make evidence look grounded."""
    d = diagnosis(evidence=[{"kind": "metric", "detail": "errors for 15 minutes above 5%"},
                            {"kind": "metric", "detail": "error_pct now 48.3%"}], confidence="medium")
    assert check_grounding(d, RESULTS).evidence_anchored == 1


def test_no_tool_results_means_nothing_is_grounded():
    g = check_grounding(diagnosis(), [])
    assert checks(g)["root_cause_seen"] is False and checks(g)["trace_ids_exist"] is False
    assert g.evidence_anchored == 0
