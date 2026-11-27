"""Hybrid retrieval over the knowledge base: keyword + vector search, fused with RRF.

    search("runbook", "checkout errors, payment charge failing", k=4)

1. **Keyword search** (Postgres full text). Good at exact words: service names,
   operation names, error messages, product IDs.
2. **Vector search** (pgvector, cosine). The query is embedded with Gemini and
   compared with every section. Good at paraphrases: "customers cannot pay" finds
   the payment runbook without sharing a word with it.
3. **Reciprocal Rank Fusion**: ``score(section) = sum over both lists of 1 / (60 + rank)``.
   Only ranks are used, so a ts_rank score and a cosine similarity never have to be
   put on the same scale. 60 is the constant from the original RRF paper (Cormack et
   al., 2009); it keeps one list's top hit from drowning the other list.
4. **Group by document**: a document's score is its best section's score, and it is
   returned with its best ``sections_per_doc`` sections. The agent gets k different
   documents, not k sections of the same file.

Modes: ``auto`` (hybrid when embeddings are available, otherwise keyword, and a
query-time embedding failure falls back to keyword with a warning), ``hybrid`` and
``vector`` (raise :class:`SearchError` instead of degrading, for measurements), and
``keyword``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from copilot.kb import embeddings, store
from copilot.kb.embeddings import EmbeddingError
from copilot.kb.store import Candidate

__all__ = ["CANDIDATES", "MODES", "RRF_K", "SearchError", "fuse", "group_by_document", "search"]

RRF_K = 60
CANDIDATES = 30                 # sections fetched from each list before fusion
MODES = ("auto", "hybrid", "keyword", "vector")
KINDS = ("runbook", "postmortem")
_POSTMORTEM_FIELDS = ("incident_id", "date", "severity", "root_cause_service", "failure_type", "duration_minutes")


class SearchError(Exception):
    """Semantic search was required (mode hybrid/vector) but is not available."""

    def __init__(self, type_: str, message: str):
        super().__init__(message)
        self.type = type_
        self.message = message


def search(kind: str, query: str, k: int, *, mode: str = "auto", sections_per_doc: int = 2,
           max_section_chars: int = 1200) -> dict[str, Any]:
    """Search one kind of document. Raises ``StoreError`` for database problems.

    Returns ``{"status", "retrieval", "result_count", "results", ["warnings"], ["message"]}``.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    warnings: list[str] = []
    try:
        embedder = embeddings.make_embedder() if mode != "keyword" else None
    except EmbeddingError as exc:                     # e.g. an invalid KB_EMBEDDING_MODEL
        if mode != "auto":
            raise SearchError(exc.type, exc.message) from None
        embedder = None
        warnings.append(f"Semantic search unavailable ({exc.type}); keyword results only.")
    if embedder is None and mode in ("hybrid", "vector"):
        raise SearchError("embedding_unavailable", "No embedding model is configured (set GOOGLE_API_KEY).")

    with store.connect(read_only=True) as conn:
        counts = store.chunk_counts(conn, embedder.model if embedder else None).get(kind, {"chunks": 0, "embedded": 0})
        if counts["chunks"] == 0:
            return {"status": "no_data", "retrieval": None, "result_count": 0, "results": [],
                    "message": f"The knowledge base has no {kind}s. Run `python -m copilot.kb.ingest`."}

        use_vector = embedder is not None and counts["embedded"] > 0
        if embedder is not None and not use_vector:
            if mode != "auto":
                raise SearchError("not_embedded", f"No {kind} is embedded with {embedder.model}; re-run ingestion.")
            warnings.append(f"No {kind} has been embedded with {embedder.model} yet; keyword results only.")

        lists: dict[str, list[Candidate]] = {}
        if mode != "vector":
            lists["keyword"] = store.keyword_candidates(conn, kind, query, CANDIDATES)
        if use_vector:
            try:
                vector = embeddings.to_pgvector(embedder.embed_query(query))
            except EmbeddingError as exc:
                if mode != "auto":
                    raise SearchError(exc.type, exc.message) from None
                warnings.append(f"Semantic search unavailable ({exc.type}); keyword results only.")
            else:
                lists["vector"] = store.vector_candidates(conn, kind, vector, embedder.model, CANDIDATES)

    retrieval = "hybrid" if len(lists) == 2 else next(iter(lists))
    results = group_by_document(fuse(lists), k, sections_per_doc, max_section_chars)
    out: dict[str, Any] = {"status": "ok" if results else "no_data", "retrieval": retrieval,
                           "result_count": len(results), "results": results}
    if not results:
        out["message"] = f"No {kind} matched the query. Try other words, for example the service name."
    if warnings:
        out["warnings"] = warnings
    return out


def fuse(lists: dict[str, Sequence[Candidate]], rrf_k: int = RRF_K) -> list[tuple[Candidate, float, frozenset[str]]]:
    """Reciprocal Rank Fusion. Returns (candidate, fused score, names of the lists it came from), best first."""
    scores: dict[str, float] = {}
    via: dict[str, set[str]] = {}
    first: dict[str, Candidate] = {}
    for name, candidates in lists.items():
        for rank, candidate in enumerate(candidates, start=1):
            scores[candidate.id] = scores.get(candidate.id, 0.0) + 1.0 / (rrf_k + rank)
            via.setdefault(candidate.id, set()).add(name)
            first.setdefault(candidate.id, candidate)
    order = sorted(scores, key=lambda cid: (-scores[cid], cid))
    return [(first[cid], scores[cid], frozenset(via[cid])) for cid in order]


def group_by_document(fused: Sequence[tuple[Candidate, float, frozenset[str]]], k: int, sections_per_doc: int = 2,
                      max_section_chars: int = 1200) -> list[dict[str, Any]]:
    """Top ``k`` documents (by their best section), each with its best sections."""
    docs: dict[str, dict[str, Any]] = {}
    for candidate, score, lists in fused:
        doc = docs.get(candidate.source)
        if doc is None:
            if len(docs) >= k:
                continue
            doc = docs[candidate.source] = _document_header(candidate, score, len(docs) + 1)
        doc["_matched_by"] |= lists
        if len(doc["sections"]) < sections_per_doc:
            text, truncated = _truncate(candidate.text, max_section_chars)
            section = {"heading": candidate.heading, "text": text}
            if truncated:
                section["truncated"] = True
            doc["sections"].append(section)
    results = []
    for doc in docs.values():
        doc["matched_by"] = sorted(doc.pop("_matched_by"))
        results.append(doc)
    return results


def _document_header(candidate: Candidate, score: float, rank: int) -> dict[str, Any]:
    header: dict[str, Any] = {"rank": rank, "source": candidate.source, "title": candidate.title,
                              "score": round(score, 5), "services": list(candidate.metadata.get("services", []))}
    if candidate.kind == "postmortem":
        header.update({key: candidate.metadata.get(key) for key in _POSTMORTEM_FIELDS})
    header["_matched_by"] = set()
    header["sections"] = []
    return header


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text[:limit].rsplit(None, 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip() + " ...", True
