"""Unit tests for the agent-facing knowledge-base tools: contract, validation, refusal, errors."""

from __future__ import annotations

import inspect
import json

import pytest

from copilot.kb import search as kb_search
from copilot.kb.store import StoreError
from copilot.tools import KB_TOOLS, search_postmortems, search_runbooks
from copilot.tools.kb import NOTE
from tests.kb_fakes import FakeKb, candidate

PAYMENT = "kb/runbooks/payment.md"


@pytest.fixture
def kb(monkeypatch) -> FakeKb:
    return FakeKb(keyword=[candidate(PAYMENT, "Diagnosis", text="Read the payment logs at warn level."),
                           candidate("kb/postmortems/inc.md", "Summary", kind="postmortem",
                                     metadata={"services": ["payment"], "incident_id": "INC-1",
                                               "root_cause_service": "payment", "failure_type": "errors"})]
                  ).install(monkeypatch)


def test_defaults_match_the_project_guide():
    assert inspect.signature(search_runbooks).parameters["k"].default == 4
    assert inspect.signature(search_postmortems).parameters["k"].default == 3
    assert [t.__name__ for t in KB_TOOLS] == ["search_runbooks", "search_postmortems"]


def test_runbook_search_returns_results_and_the_evidence_note(kb):
    out = search_runbooks("payment charge errors")
    assert out["status"] == "ok" and out["kind"] == "runbook" and out["retrieval"] == "keyword"
    assert out["results"][0]["source"] == PAYMENT and out["results"][0]["sections"][0]["heading"] == "Diagnosis"
    assert out["note"] == NOTE and "not evidence" in NOTE
    json.dumps(out, allow_nan=False)


def test_postmortem_search_returns_incident_fields(kb):
    result = search_postmortems("checkout failing at payment")["results"][0]
    assert result["incident_id"] == "INC-1" and result["root_cause_service"] == "payment"


def test_query_whitespace_is_normalised_before_searching(kb):
    search_runbooks("  payment \n\t charge   errors ")
    assert ("keyword", "runbook", "payment charge errors", kb_search.CANDIDATES) in kb.calls


@pytest.mark.parametrize("query", [None, "", "   ", 42, ["payment"], "x" * 501, "pay\x00ment"])
def test_invalid_queries_are_rejected_before_any_database_access(kb, query):
    out = search_runbooks(query)
    assert out["status"] == "error" and out["error"]["type"] == "invalid_input"
    assert kb.connections == []


@pytest.mark.parametrize("k", [0, 9, -1, True, "3", 2.5, None])
def test_invalid_k_is_rejected(kb, k):
    out = search_postmortems("payment", k)
    assert out["error"]["type"] == "invalid_input" and kb.connections == []


@pytest.mark.parametrize("query", ["flagd outage", "which feature flag is on", "paymentFailure",
                                   "OpenFeature provider errors", "is the flagservice down"])
def test_queries_about_fault_injection_are_refused_and_not_echoed(kb, query):
    out = search_runbooks(query)
    assert out["status"] == "error" and out["error"]["type"] == "forbidden_query"
    assert kb.connections == []
    assert query not in json.dumps(out)


def test_unreachable_database_is_a_typed_error(kb_offline):
    out = search_runbooks("checkout errors")
    assert out["status"] == "error" and out["error"]["type"] == "connection"
    assert "docker compose up -d kb-postgres" in out["error"]["message"] and kb_offline == [True]


def test_uninitialised_knowledge_base_is_reported(monkeypatch):
    def not_ready(*a, **kw):
        raise StoreError("not_initialized", "The knowledge base is not set up yet. Run `python -m copilot.kb.ingest`.")

    monkeypatch.setattr(kb_search, "search", not_ready)
    assert search_postmortems("payment")["error"]["type"] == "not_initialized"


def test_unexpected_exceptions_become_internal_errors(monkeypatch):
    monkeypatch.setattr(kb_search, "search", lambda *a, **kw: 1 / 0)
    out = search_runbooks("payment")
    assert out["status"] == "error" and out["error"]["type"] == "internal" and "ZeroDivisionError" in out["error"]["message"]


def test_results_are_scrubbed_even_if_a_document_slipped_through(monkeypatch):
    """Defence in depth: ingestion refuses such text, but the tool output is scrubbed anyway."""
    leaky = candidate(PAYMENT, "Diagnosis", text="Check flagd and the paymentFailure switch.",
                      metadata={"services": ["payment"], "feature_flag.key": "paymentFailure"})
    FakeKb(keyword=[leaky]).install(monkeypatch)
    text = json.dumps(search_runbooks("payment errors")).lower()
    assert "flagd" not in text and "paymentfailure" not in text and "[redacted]" in text


def test_no_data_has_no_note(monkeypatch):
    FakeKb(keyword=[], counts={"runbook": {"chunks": 3, "embedded": 0}}).install(monkeypatch)
    out = search_runbooks("zzz")
    assert out["status"] == "no_data" and "note" not in out
