"""Unit tests for the Gemini embedder, with a fake google-genai client (no network)."""

from __future__ import annotations

import math
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors

from copilot.kb import embeddings
from copilot.kb.embeddings import (
    EMBEDDING_DIM, EmbeddingError, GeminiEmbedder, gemini_configured, normalize, to_pgvector,
)

# Captured at import time: conftest replaces embeddings.make_embedder during every test.
REAL_MAKE_EMBEDDER = embeddings.make_embedder


class FakeModels:
    def __init__(self, script):
        self.script = list(script)      # one entry per call: a response, or an exception to raise
        self.calls: list[dict] = []

    def embed_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": list(contents), "config": config})
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        return step(contents) if callable(step) else step


def ok(contents, dim=EMBEDDING_DIM, value=3.0):
    return SimpleNamespace(embeddings=[SimpleNamespace(values=[value] * dim) for _ in contents])


def embedder(*script) -> tuple[GeminiEmbedder, FakeModels]:
    models = FakeModels(script or [ok])
    return GeminiEmbedder(model="gemini-embedding-001", client=SimpleNamespace(models=models)), models


def api_error(code: int, message: str = "boom", *, retry_delay: str | None = None,
              quota_id: str | None = None) -> errors.APIError:
    cls = errors.ClientError if code < 500 else errors.ServerError
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]})
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return cls(code, {"error": {"code": code, "message": message, "status": "X", "details": details}})


SLEEPS: list[float] = []


@pytest.fixture(autouse=True)
def _fresh_cache_and_no_sleep(monkeypatch):
    embeddings._QUERY_CACHE.clear()
    SLEEPS.clear()
    monkeypatch.setattr(embeddings.time, "sleep", SLEEPS.append)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_no_key_means_no_embedder():
    assert gemini_configured() is False and REAL_MAKE_EMBEDDER() is None


@pytest.mark.parametrize("var, value", [("GOOGLE_API_KEY", "k"), ("GEMINI_API_KEY", "k"),
                                        ("GOOGLE_GENAI_USE_VERTEXAI", "true")])
def test_a_key_or_vertex_gives_a_gemini_embedder(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    made = REAL_MAKE_EMBEDDER()
    assert isinstance(made, GeminiEmbedder) and made.model == "gemini-embedding-001"


def test_blank_key_does_not_count(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "  ")
    assert gemini_configured() is False


def test_model_comes_from_the_environment_and_is_validated(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setenv("KB_EMBEDDING_MODEL", "text-embedding-005")
    assert REAL_MAKE_EMBEDDER().model == "text-embedding-005"
    monkeypatch.setenv("KB_EMBEDDING_MODEL", "bad model; rm -rf")
    with pytest.raises(EmbeddingError) as info:
        REAL_MAKE_EMBEDDER()
    assert info.value.type == "configuration"


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
def test_documents_are_batched_and_use_the_document_task_type():
    emb, models = embedder(ok)
    vectors = emb.embed_documents([f"text {i}" for i in range(120)])
    assert [len(c["contents"]) for c in models.calls] == [50, 50, 20]
    config = models.calls[0]["config"]
    assert config.task_type == "RETRIEVAL_DOCUMENT" and config.output_dimensionality == EMBEDDING_DIM
    assert len(vectors) == 120 and all(len(v) == EMBEDDING_DIM for v in vectors)
    assert math.isclose(sum(x * x for x in vectors[0]), 1.0)          # truncated output is re-normalised


def test_query_uses_the_query_task_type_and_is_cached():
    emb, models = embedder(ok)
    first = emb.embed_query("checkout errors")
    second = emb.embed_query("checkout errors")
    assert first == second and len(models.calls) == 1
    assert models.calls[0]["config"].task_type == "RETRIEVAL_QUERY"
    second[0] = 99.0                                                  # callers cannot corrupt the cache
    assert emb.embed_query("checkout errors")[0] != 99.0


def test_query_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(embeddings, "QUERY_CACHE_SIZE", 3)
    emb, _ = embedder(ok)
    for i in range(5):
        emb.embed_query(f"q{i}")
    assert len(embeddings._QUERY_CACHE) == 3


@pytest.mark.parametrize("response, problem", [
    (lambda c: ok(c, dim=3072), "dimensions"),
    (lambda c: SimpleNamespace(embeddings=[]), "Expected 1 embeddings"),
    (lambda c: ok(c, value=float("nan")), "non-finite"),
    (lambda c: ok(c, value=0.0), "zero vector"),
])
def test_bad_responses_are_rejected(response, problem):
    emb, _ = embedder(response)
    with pytest.raises(EmbeddingError) as info:
        emb.embed_query("x")
    assert info.value.type == "invalid_response" and problem in info.value.message


# --------------------------------------------------------------------------- #
# Errors and retries
# --------------------------------------------------------------------------- #
def test_rate_limits_are_retried_for_documents():
    emb, models = embedder(api_error(429), api_error(429), ok)
    assert len(emb.embed_documents(["a", "b"])) == 2 and len(models.calls) == 3


def test_queries_are_never_retried():
    emb, models = embedder(api_error(429), ok)
    with pytest.raises(EmbeddingError) as info:
        emb.embed_query("x")
    assert info.value.type == "quota" and len(models.calls) == 1


def test_retries_give_up_after_the_limit():
    emb, models = embedder(api_error(503))
    with pytest.raises(EmbeddingError) as info:
        emb.embed_documents(["a"])
    assert info.value.type == "unavailable" and len(models.calls) == embeddings.DOCUMENT_RETRIES + 1


@pytest.mark.parametrize("code, type_", [(401, "auth"), (403, "auth"), (404, "model_not_found"),
                                         (400, "invalid_request")])
def test_client_errors_are_typed_and_not_retried(code, type_):
    emb, models = embedder(api_error(code))
    with pytest.raises(EmbeddingError) as info:
        emb.embed_documents(["a"])
    assert info.value.type == type_ and len(models.calls) == 1


@pytest.mark.parametrize("fake_key", ["AIzaFAKE-not-a-real-key-0123456789",          # deliberately fake
                                      "AQ.FAKE-not.a-real-key_0123456789abcdef"])     # newer key format
def test_api_keys_never_appear_in_error_messages(fake_key):
    emb, _ = embedder(api_error(400, f"API key not valid: {fake_key}"))
    with pytest.raises(EmbeddingError) as info:
        emb.embed_query("x")
    assert fake_key[:4] not in info.value.message and "[key]" in info.value.message


@pytest.mark.parametrize("exc, type_", [(httpx.ReadTimeout("slow"), "timeout"),
                                        (httpx.ConnectError("refused"), "connection")])
def test_transport_failures_are_typed(exc, type_):
    emb, _ = embedder(exc)
    with pytest.raises(EmbeddingError) as info:
        emb.embed_query("x")
    assert info.value.type == type_


# --------------------------------------------------------------------------- #
# Vector helpers
# --------------------------------------------------------------------------- #
def test_to_pgvector_formats_and_checks():
    text = to_pgvector([0.5] * EMBEDDING_DIM)
    assert text.startswith("[0.5,") and text.endswith("]") and text.count(",") == EMBEDDING_DIM - 1
    with pytest.raises(EmbeddingError):
        to_pgvector([0.5] * 10)
    with pytest.raises(EmbeddingError):
        to_pgvector([float("inf")] * EMBEDDING_DIM)


def test_normalize():
    assert normalize([3.0, 4.0]) == [0.6, 0.8]
    with pytest.raises(EmbeddingError):
        normalize([])


def test_rate_limits_wait_as_long_as_the_api_asks_and_report_progress():
    emb, models = embedder(api_error(429, retry_delay="41.5s", quota_id="EmbedPerMinutePerProject-FreeTier"), ok)
    waits = []
    emb.on_wait = lambda seconds, why: waits.append((seconds, why))
    assert len(emb.embed_documents(["a"])) == 1 and len(models.calls) == 2
    assert SLEEPS == [41.5] and waits == [(41.5, "quota")]


def test_waits_are_capped_and_fall_back_to_backoff():
    emb, _ = embedder(api_error(429, retry_delay="900s"), api_error(503), ok)
    emb.embed_documents(["a"])
    assert SLEEPS == [embeddings.MAX_WAIT_SECONDS, 4.0]          # capped server delay, then plain back-off


def test_an_exhausted_daily_quota_is_reported_at_once():
    emb, models = embedder(api_error(429, retry_delay="30s", quota_id="EmbedContentRequestsPerDayPerProject-FreeTier"))
    with pytest.raises(EmbeddingError) as info:
        emb.embed_documents(["a"])
    assert info.value.type == "quota_daily" and "tomorrow" in info.value.message
    assert len(models.calls) == 1 and SLEEPS == []


def test_malformed_error_details_are_ignored():
    err = errors.ClientError(429, {"error": {"code": 429, "message": "slow down", "details": [
        "junk", {"@type": "x.RetryInfo", "retryDelay": "soon"}, {"@type": "x.QuotaFailure", "violations": "bad"}]}})
    emb, _ = embedder(err, ok)
    emb.embed_documents(["a"])
    assert SLEEPS == [2.0]


def test_warm_queries_batches_once_and_then_serves_from_the_cache():
    emb, models = embedder(ok)
    emb.warm_queries(["checkout errors", "payment errors", "checkout errors"])      # duplicates embedded once
    assert len(models.calls) == 1 and models.calls[0]["contents"] == ["checkout errors", "payment errors"]
    assert models.calls[0]["config"].task_type == "RETRIEVAL_QUERY"
    emb.embed_query("payment errors")
    emb.warm_queries(["payment errors"])
    assert len(models.calls) == 1                                                    # all from the cache
