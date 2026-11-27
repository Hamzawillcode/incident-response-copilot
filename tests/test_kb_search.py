"""Unit tests for hybrid retrieval (fusion, grouping, modes, fallbacks) with an in-memory store."""

from __future__ import annotations

import pytest

from copilot.kb import embeddings, search as kb
from copilot.kb.search import SearchError, fuse, group_by_document, search
from tests.kb_fakes import FailingEmbedder, FakeKb, HashingEmbedder, candidate

PAYMENT = "kb/runbooks/payment.md"
CHECKOUT = "kb/runbooks/checkout.md"
CART = "kb/runbooks/cart.md"


def use_embedder(monkeypatch, embedder):
    monkeypatch.setattr(embeddings, "make_embedder", lambda: embedder)
    return embedder


# --------------------------------------------------------------------------- #
# Reciprocal Rank Fusion
# --------------------------------------------------------------------------- #
def test_rrf_rewards_chunks_found_by_both_lists():
    a, b, c = candidate(PAYMENT), candidate(CHECKOUT), candidate(CART)
    fused = fuse({"keyword": [a, b], "vector": [c, a]})
    assert [f[0].source for f in fused] == [PAYMENT, CART, CHECKOUT]
    score = {f[0].source: f[1] for f in fused}
    assert score[PAYMENT] == pytest.approx(1 / 61 + 1 / 62)
    assert score[CART] == pytest.approx(1 / 61) and score[CHECKOUT] == pytest.approx(1 / 62)
    assert fused[0][2] == {"keyword", "vector"} and fused[1][2] == {"vector"}


def test_rrf_ignores_raw_scores_and_breaks_ties_by_id():
    a = candidate(PAYMENT, score=0.0001)
    b = candidate(CHECKOUT, score=999.0)
    fused = fuse({"keyword": [a], "vector": [b]})                   # both rank 1 in their list
    assert fused[0][1] == fused[1][1]
    assert [f[0].id for f in fused] == sorted([a.id, b.id])


# --------------------------------------------------------------------------- #
# Grouping by document
# --------------------------------------------------------------------------- #
def test_results_are_documents_with_their_best_sections():
    chunks = [candidate(PAYMENT, "Diagnosis", position=2), candidate(CHECKOUT, "Symptoms"),
              candidate(PAYMENT, "Symptoms", position=1), candidate(PAYMENT, "Remediation", position=4),
              candidate(CART, "Overview")]
    docs = group_by_document(fuse({"keyword": chunks}), k=2, sections_per_doc=2)
    assert [d["source"] for d in docs] == [PAYMENT, CHECKOUT]       # cart is beyond k
    assert [s["heading"] for s in docs[0]["sections"]] == ["Diagnosis", "Symptoms"]   # best first, capped
    assert [d["rank"] for d in docs] == [1, 2]
    assert docs[0]["score"] == round(1 / 61, 5) and docs[0]["matched_by"] == ["keyword"]
    assert docs[0]["services"] == ["payment"] and "_matched_by" not in docs[0]


def test_postmortem_results_carry_incident_metadata():
    meta = {"services": ["payment"], "incident_id": "INC-1", "date": "2026-03-14", "severity": "SEV-2",
            "root_cause_service": "payment", "failure_type": "errors", "duration_minutes": 47}
    hit = candidate("kb/postmortems/inc-1.md", "Summary", kind="postmortem", metadata=meta)
    doc = group_by_document(fuse({"keyword": [hit]}), k=3)[0]
    assert doc["incident_id"] == "INC-1" and doc["root_cause_service"] == "payment" and doc["failure_type"] == "errors"


def test_long_sections_are_truncated_on_a_word_boundary():
    hit = candidate(PAYMENT, text="word " * 400)
    section = group_by_document(fuse({"keyword": [hit]}), k=1, max_section_chars=100)[0]["sections"][0]
    assert section["truncated"] is True and len(section["text"]) <= 104 and section["text"].endswith("word ...")


# --------------------------------------------------------------------------- #
# Modes and fallbacks
# --------------------------------------------------------------------------- #
def test_without_an_embedder_search_is_keyword_only(monkeypatch):
    fake = FakeKb(keyword=[candidate(PAYMENT)]).install(monkeypatch)
    out = search("runbook", "payment errors", 4)
    assert out["status"] == "ok" and out["retrieval"] == "keyword" and "warnings" not in out
    assert [c[0] for c in fake.calls] == ["counts", "keyword"]
    assert fake.connections == [True]                                # read-only session


def test_with_embeddings_search_is_hybrid(monkeypatch):
    emb = use_embedder(monkeypatch, HashingEmbedder())
    fake = FakeKb(keyword=[candidate(CHECKOUT)], vector=[candidate(PAYMENT)]).install(monkeypatch)
    out = search("runbook", "customers cannot pay", 4)
    assert out["retrieval"] == "hybrid" and {r["source"] for r in out["results"]} == {PAYMENT, CHECKOUT}
    assert ("vector", "runbook", emb.model, kb.CANDIDATES) in fake.calls
    assert emb.queries == ["customers cannot pay"]


def test_nothing_embedded_with_this_model_means_keyword_and_no_api_call(monkeypatch):
    emb = use_embedder(monkeypatch, HashingEmbedder())
    FakeKb(keyword=[candidate(PAYMENT)], counts={"runbook": {"chunks": 5, "embedded": 0}}).install(monkeypatch)
    out = search("runbook", "payment", 4)
    assert out["retrieval"] == "keyword" and "embedded with" in out["warnings"][0]
    assert emb.queries == []                                         # no wasted embedding call


def test_embedding_failure_falls_back_to_keyword_with_a_warning(monkeypatch):
    use_embedder(monkeypatch, FailingEmbedder("quota"))
    FakeKb(keyword=[candidate(PAYMENT)], vector=[candidate(CHECKOUT)]).install(monkeypatch)
    out = search("runbook", "payment", 4)
    assert out["status"] == "ok" and out["retrieval"] == "keyword"
    assert out["warnings"] == ["Semantic search unavailable (quota); keyword results only."]


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_explicit_semantic_modes_fail_instead_of_degrading(monkeypatch, mode):
    FakeKb(keyword=[candidate(PAYMENT)]).install(monkeypatch)
    with pytest.raises(SearchError) as info:
        search("runbook", "payment", 4, mode=mode)                   # no embedder configured
    assert info.value.type == "embedding_unavailable"
    use_embedder(monkeypatch, FailingEmbedder("timeout"))
    FakeKb(keyword=[candidate(PAYMENT)], vector=[candidate(PAYMENT)]).install(monkeypatch)
    with pytest.raises(SearchError) as info:
        search("runbook", "payment", 4, mode=mode)
    assert info.value.type == "timeout"


def test_vector_mode_uses_only_the_vector_list(monkeypatch):
    use_embedder(monkeypatch, HashingEmbedder())
    fake = FakeKb(keyword=[candidate(CHECKOUT)], vector=[candidate(PAYMENT)]).install(monkeypatch)
    out = search("runbook", "payment", 4, mode="vector")
    assert out["retrieval"] == "vector" and [r["source"] for r in out["results"]] == [PAYMENT]
    assert not any(c[0] == "keyword" for c in fake.calls)


def test_embedder_configuration_error_degrades_to_keyword(monkeypatch):
    from copilot.kb.embeddings import EmbeddingError

    def bad_config():
        raise EmbeddingError("configuration", "KB_EMBEDDING_MODEL is not a valid model name.")

    monkeypatch.setattr(embeddings, "make_embedder", bad_config)
    FakeKb(keyword=[candidate(PAYMENT)]).install(monkeypatch)
    out = search("runbook", "payment", 4)
    assert out["retrieval"] == "keyword" and "(configuration)" in out["warnings"][0]
    with pytest.raises(SearchError):
        search("runbook", "payment", 4, mode="hybrid")


def test_keyword_mode_never_asks_for_an_embedder(monkeypatch):
    def boom():
        raise AssertionError("keyword mode must not create an embedder")

    monkeypatch.setattr(embeddings, "make_embedder", boom)
    FakeKb(keyword=[candidate(PAYMENT)]).install(monkeypatch)
    assert search("runbook", "payment", 4, mode="keyword")["retrieval"] == "keyword"


def test_empty_knowledge_base_explains_how_to_fill_it(monkeypatch):
    FakeKb(counts={}).install(monkeypatch)
    out = search("postmortem", "payment", 3)
    assert out["status"] == "no_data" and "copilot.kb.ingest" in out["message"]


def test_no_match_is_no_data_with_a_hint(monkeypatch):
    FakeKb(keyword=[], counts={"runbook": {"chunks": 5, "embedded": 0}}).install(monkeypatch)
    out = search("runbook", "zzz", 4)
    assert out["status"] == "no_data" and out["results"] == [] and "service name" in out["message"]


def test_kind_filter_is_applied(monkeypatch):
    FakeKb(keyword=[candidate(PAYMENT), candidate("kb/postmortems/x.md", kind="postmortem")]).install(monkeypatch)
    assert [r["source"] for r in search("postmortem", "payment", 3)["results"]] == ["kb/postmortems/x.md"]


@pytest.mark.parametrize("kind, mode", [("howto", "auto"), ("runbook", "fuzzy")])
def test_invalid_kind_or_mode_is_a_programming_error(kind, mode):
    with pytest.raises(ValueError):
        search(kind, "x", 3, mode=mode)
