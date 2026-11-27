"""Test doubles for the knowledge base: offline embedders and an in-memory store."""

from __future__ import annotations

import contextlib
import hashlib
import re
from typing import Any

from copilot.kb import store
from copilot.kb.embeddings import EMBEDDING_DIM, EmbeddingError, normalize
from copilot.kb.store import Candidate


class HashingEmbedder:
    """Deterministic stand-in for Gemini: a hashed bag of words, 768 dims, unit length.

    Texts that share words get similar vectors, so vector search behaves sensibly in
    tests. It knows nothing about meaning; it exists to exercise the pgvector code path
    without network access.
    """

    def __init__(self, model: str = "test-hashing-embedder"):
        self.model = model
        self.document_batches: list[int] = []
        self.queries: list[str] = []

    def embed_documents(self, texts):
        self.document_batches.append(len(texts))
        return [self.vector(t) for t in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return self.vector(text)

    @staticmethod
    def vector(text: str) -> list[float]:
        values = [0.0] * EMBEDDING_DIM
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            h = int.from_bytes(hashlib.sha256(word.encode()).digest()[:8], "big")
            values[h % EMBEDDING_DIM] += 1.0 if (h >> 40) & 1 else -1.0
        if not any(values):
            values[0] = 1.0
        return normalize(values)


class FailingEmbedder:
    """Every call fails like a rate-limited Gemini API."""

    def __init__(self, type_: str = "quota", model: str = "test-hashing-embedder"):
        self.type = type_
        self.model = model
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        raise EmbeddingError(self.type, "simulated embedding failure")

    def embed_query(self, text):
        self.calls += 1
        raise EmbeddingError(self.type, "simulated embedding failure")


def candidate(source: str, heading: str = "Symptoms", *, kind: str = "runbook", text: str = "Some text.",
              position: int = 0, score: float = 0.5, metadata: dict[str, Any] | None = None,
              title: str | None = None) -> Candidate:
    """A search hit for document ``source`` (e.g. "kb/runbooks/payment.md")."""
    doc_id = source.rsplit("/", 1)[-1].removesuffix(".md")
    return Candidate(id=f"{kind}/{doc_id}#{position:02d}-{heading.lower()}", kind=kind, doc_id=doc_id,
                     source=source, title=title or doc_id.title(), heading=heading, position=position, text=text,
                     score=score, metadata=metadata if metadata is not None else {"services": [doc_id]})


class FakeKb:
    """Replaces the database functions of ``copilot.kb.store`` and records every call."""

    def __init__(self, keyword=(), vector=(), counts: dict[str, dict[str, int]] | None = None):
        self.keyword = list(keyword)
        self.vector = list(vector)
        self.counts = counts
        self.connections: list[bool] = []
        self.calls: list[tuple] = []

    def install(self, monkeypatch) -> FakeKb:
        monkeypatch.setattr(store, "connect", self.connect)
        monkeypatch.setattr(store, "chunk_counts", self.chunk_counts)
        monkeypatch.setattr(store, "keyword_candidates", self.keyword_candidates)
        monkeypatch.setattr(store, "vector_candidates", self.vector_candidates)
        return self

    def connect(self, *, read_only: bool = True):
        self.connections.append(read_only)
        return contextlib.nullcontext(object())

    def chunk_counts(self, conn, model):
        self.calls.append(("counts", model))
        if self.counts is not None:
            return self.counts
        kinds = {c.kind for c in self.keyword + self.vector} or {"runbook", "postmortem"}
        return {kind: {"chunks": 10, "embedded": 10 if self.vector else 0} for kind in kinds}

    def keyword_candidates(self, conn, kind, query, limit):
        self.calls.append(("keyword", kind, query, limit))
        return [c for c in self.keyword if c.kind == kind][:limit]

    def vector_candidates(self, conn, kind, vector, model, limit):
        self.calls.append(("vector", kind, model, limit))
        assert isinstance(vector, str) and vector.startswith("[")      # pgvector text form
        return [c for c in self.vector if c.kind == kind][:limit]
