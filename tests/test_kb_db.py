"""Integration tests against the real knowledge-base Postgres (pgvector), in a private schema.

Run `docker compose up -d kb-postgres` first; otherwise these tests are skipped.
Embeddings come from the offline HashingEmbedder: these tests check the SQL and the
pgvector code path, not the quality of Gemini embeddings.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import psycopg
import pytest
import yaml

from copilot.kb import embeddings, store
from copilot.kb.documents import DocumentError, load_chunks
from copilot.kb.embeddings import EmbeddingError
from copilot.kb.ingest import ingest
from copilot.kb.search import search
from copilot.tools import search_postmortems, search_runbooks
from tests.kb_fakes import FailingEmbedder, HashingEmbedder

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "kb"
QUERIES = yaml.safe_load((ROOT / "eval" / "retrieval_queries.yaml").read_text())


def stems(result: dict) -> list[str]:
    return [Path(r["source"]).stem for r in result["results"]]


def count_rows(where: str = "true") -> int:
    with store.connect() as conn:
        return conn.execute(f"SELECT count(*) FROM {store.qualified_table()} WHERE {where}").fetchone()[0]


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
def test_ingest_creates_the_table_and_is_idempotent(kb_db):
    chunks = load_chunks(KB_DIR)
    first = ingest(KB_DIR)
    assert first.inserted == len(chunks) and first.embedded == 0
    assert first.stored["runbook"]["documents"] == 14 and first.stored["postmortem"]["documents"] == 5
    second = ingest(KB_DIR)
    assert (second.inserted, second.updated, second.deleted, second.unchanged) == (0, 0, 0, len(chunks))


def test_changed_and_removed_sections_are_synchronised(kb_db, tmp_path):
    kb = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb)
    ingest(kb)
    payment = kb / "runbooks" / "payment.md"
    original = payment.read_text()
    edited = original.replace("restart payment,", "restart the payment service,")
    assert edited != original                                         # the edit must really change one section
    payment.write_text(edited)
    removed = kb / "postmortems" / "2026-09-23-false-alarm-error-ratio.md"
    removed_sections = len([c for c in load_chunks(kb) if c.source.endswith(removed.name)])
    removed.unlink()
    report = ingest(kb)
    assert report.updated == 1 and report.inserted == 0 and report.deleted == removed_sections
    assert count_rows(f"source = 'kb/postmortems/{removed.name}'") == 0


def test_embeddings_are_added_later_and_only_where_needed(kb_db):
    total = len(load_chunks(KB_DIR))
    ingest(KB_DIR)                                                   # keyword-only first (no key yet)
    emb = HashingEmbedder()
    assert ingest(KB_DIR, embedder=emb).embedded == total             # key added: everything embedded once
    assert ingest(KB_DIR, embedder=emb).embedded == 0                 # nothing changed: no API calls
    assert ingest(KB_DIR, embedder=HashingEmbedder("other-model")).embedded == total   # model changed
    assert count_rows("embedding_model = 'other-model'") == total
    assert ingest(KB_DIR).embedded == 0 and count_rows("embedding IS NOT NULL") == total  # kept without a key


def test_a_failed_embedding_run_writes_nothing(kb_db):
    with pytest.raises(EmbeddingError):
        ingest(KB_DIR, embedder=FailingEmbedder())
    assert count_rows() == 0


def test_a_document_with_fault_injection_terms_stops_ingestion_before_any_write(kb_db, tmp_path):
    kb = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb)
    ingest(kb)
    before = count_rows()
    (kb / "runbooks" / "sneaky.md").write_text("---\nservices: [payment]\n---\n# Sneaky\n## Fix\nTurn off flagd.\n")
    with pytest.raises(DocumentError):
        ingest(kb)
    assert count_rows() == before


def test_rebuild_only_touches_its_own_schema(kb_db):
    """--rebuild on an empty KB_SCHEMA must not fall through search_path to public.kb_chunks."""
    report = ingest(KB_DIR, rebuild=True)
    assert report.inserted == report.chunks
    with store.connect() as conn:
        assert conn.execute("SELECT current_setting('search_path')").fetchone()[0].startswith(kb_db)


# --------------------------------------------------------------------------- #
# Keyword search (what works today, without a Gemini key)
# --------------------------------------------------------------------------- #
def test_week3_done_criterion_checkout_errors_finds_the_checkout_runbook_in_the_top_3(kb_db):
    ingest(KB_DIR)
    assert "checkout" in stems(search("runbook", "checkout errors", 3, mode="keyword"))


def test_guide_check_payment_charge_errors_returns_the_payment_runbook_first(kb_db):
    ingest(KB_DIR)
    assert stems(search("runbook", "payment charge errors", 4, mode="keyword"))[0] == "payment"


def test_keyword_style_queries_find_their_document_in_the_top_3(kb_db):
    ingest(KB_DIR)
    rows = [(kind, q) for kind, items in QUERIES.items() for q in items if q["style"] == "keyword"]
    misses = [q["query"] for kind, q in rows if not set(stems(search(kind, q["query"], 3, mode="keyword")))
              & set(q["expect"])]
    assert len(misses) <= len(rows) // 10, misses                     # at least 90 % hit@3


@pytest.mark.parametrize("query", ["'; DROP TABLE kb_chunks; --", "a & b | !c <-> d:*", "(((", "the and of",
                                   "ünïcödé ✓", "%(query)s"])
def test_hostile_query_text_is_just_text(kb_db, query):
    ingest(KB_DIR)
    out = search_runbooks(query)
    assert out["status"] in ("ok", "no_data")
    assert count_rows() == len(load_chunks(KB_DIR))


# --------------------------------------------------------------------------- #
# Vector and hybrid search (pgvector path, with the offline embedder)
# --------------------------------------------------------------------------- #
def test_hybrid_search_uses_pgvector(kb_db, monkeypatch):
    emb = HashingEmbedder()
    ingest(KB_DIR, embedder=emb)
    monkeypatch.setattr(embeddings, "make_embedder", lambda: emb)
    out = search("runbook", "payment charge errors", 4)
    assert out["retrieval"] == "hybrid" and stems(out)[0] == "payment"
    assert "vector" in out["results"][0]["matched_by"]
    vec = search("runbook", "payment charge errors", 4, mode="vector")
    assert vec["retrieval"] == "vector" and "payment" in stems(vec)


def test_vectors_from_another_model_are_never_compared(kb_db, monkeypatch):
    ingest(KB_DIR, embedder=HashingEmbedder("model-a"))
    monkeypatch.setattr(embeddings, "make_embedder", lambda: HashingEmbedder("model-b"))
    out = search("runbook", "payment charge errors", 4)
    assert out["retrieval"] == "keyword" and "model-b" in out["warnings"][0]


# --------------------------------------------------------------------------- #
# The tools end to end, and the read-only session
# --------------------------------------------------------------------------- #
def test_tools_end_to_end(kb_db):
    ingest(KB_DIR)
    runbooks = search_runbooks("checkout errors")
    assert runbooks["status"] == "ok" and stems(runbooks)[0] == "checkout"
    assert len(json.dumps(runbooks)) < 16_000                         # a summary, not a dump
    postmortems = search_postmortems("checkout failing because the cart store was down")
    assert postmortems["results"][0]["root_cause_service"] == "valkey-cart"


def test_search_before_ingestion_says_how_to_set_up(kb_db):
    out = search_runbooks("checkout errors")
    assert out["status"] == "error" and out["error"]["type"] == "not_initialized"
    assert "copilot.kb.ingest" in out["error"]["message"]


def test_search_sessions_are_read_only_with_a_statement_timeout(kb_db):
    ingest(KB_DIR)
    with store.connect(read_only=True) as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "5s"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(f"DELETE FROM {store.qualified_table()}")
    assert count_rows() == len(load_chunks(KB_DIR))
