"""Text embeddings for the knowledge base (Gemini), behind a small interface tests can replace.

    embedder = make_embedder()          # GeminiEmbedder, or None when no Gemini key is configured
    embedder.embed_documents([...])     # task type RETRIEVAL_DOCUMENT, batched
    embedder.embed_query("...")         # task type RETRIEVAL_QUERY, cached, never retried

Design notes
------------
* Documents and queries are embedded with different task types. Gemini's embedding
  models are trained for this asymmetric setup (a short question vs. a paragraph).
* ``output_dimensionality=768``: Gemini embedding models return 3072 dimensions by
  default and support shorter, Matryoshka-style truncation. 768 keeps the table small
  and stays under pgvector's 2,000-dimension limit for indexes. Truncated vectors are
  not unit length, so they are L2-normalised here.
* ``make_embedder()`` returns ``None`` when no key is configured. Ingestion then stores
  text only and search runs keyword-only, so the knowledge base works before a key
  exists and keeps working if the embedding API is down during an incident.
* Query embedding is a single attempt with a short timeout (the caller falls back to
  keyword search). Document embedding during ingestion retries rate limits, waiting as
  long as the API's ``RetryInfo`` asks (free-tier limits are per minute); an exhausted
  *daily* quota is reported at once instead of retried.
* The query text is sent to Google, the same provider that already receives every
  prompt and tool result from the agent, so no new party sees incident data.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from copilot.tools._http import float_from_env

__all__ = ["DEFAULT_MODEL", "EMBEDDING_DIM", "Embedder", "EmbeddingError", "GeminiEmbedder", "embedding_model",
           "gemini_configured", "make_embedder", "normalize", "to_pgvector"]

EMBEDDING_DIM = 768
DEFAULT_MODEL = "gemini-embedding-001"
BATCH_SIZE = 50                 # texts per request (the API accepts up to 100)
DOCUMENT_RETRIES = 5            # rate limits / server errors during ingestion
MAX_WAIT_SECONDS = 65.0         # longest single wait between retries (free-tier limits are per minute)
_RETRYABLE = frozenset({"quota", "unavailable", "timeout", "connection"})
QUERY_CACHE_SIZE = 256
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-/]{0,99}")
# Classic Google API keys ("AIza" + 35) and the newer "AQ." format; removed from any error message.
_API_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|AQ\.[0-9A-Za-z_\-.]{20,}")


class EmbeddingError(Exception):
    """Embedding failed. ``type``: quota | quota_daily | auth | invalid_request | model_not_found |
    unavailable | timeout | connection | invalid_response | configuration.

    ``retry_after``: seconds the API asked us to wait (rate limits), if it said so.
    """

    def __init__(self, type_: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.type = type_
        self.message = message
        self.retry_after = retry_after


class Embedder(Protocol):
    model: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def embedding_model() -> str:
    model = os.environ.get("KB_EMBEDDING_MODEL", "").strip() or DEFAULT_MODEL
    if not _MODEL_NAME.fullmatch(model):
        raise EmbeddingError("configuration", "KB_EMBEDDING_MODEL is not a valid model name.")
    return model


def gemini_configured() -> bool:
    """True when google-genai can authenticate: an API key, or Vertex AI explicitly enabled."""
    if os.environ.get("GOOGLE_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip():
        return True
    return os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in ("1", "true", "yes")


def make_embedder() -> Embedder | None:
    """The configured embedder, or ``None`` (keyword-only knowledge base). Tests replace this."""
    if not gemini_configured():
        return None
    return GeminiEmbedder(model=embedding_model())


# --------------------------------------------------------------------------- #
# Vectors
# --------------------------------------------------------------------------- #
def normalize(vector: Sequence[float]) -> list[float]:
    values = [float(v) for v in vector]
    if not values or not all(math.isfinite(v) for v in values):
        raise EmbeddingError("invalid_response", "The embedding contains non-finite values.")
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        raise EmbeddingError("invalid_response", "The embedding is a zero vector.")
    return [v / norm for v in values]


def to_pgvector(vector: Sequence[float]) -> str:
    """Text form accepted by pgvector ('[0.1,0.2,...]'); checks dimension and finiteness."""
    if len(vector) != EMBEDDING_DIM:
        raise EmbeddingError("invalid_response", f"Expected {EMBEDDING_DIM} dimensions, got {len(vector)}.")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector):
        raise EmbeddingError("invalid_response", "The embedding contains non-finite values.")
    return "[" + ",".join(format(float(v), ".8g") for v in vector) + "]"


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #
_QUERY_CACHE: OrderedDict[tuple[str, str], list[float]] = OrderedDict()


class GeminiEmbedder:
    """Embeddings from the Gemini API (AI Studio key, or Vertex AI)."""

    def __init__(self, model: str | None = None, *, timeout_seconds: float | None = None, client: Any = None,
                 on_wait: Callable[[float, str], None] | None = None):
        self.model = model or embedding_model()
        self.timeout_seconds = timeout_seconds or float_from_env("KB_EMBEDDING_TIMEOUT_SECONDS", 10.0, 0.0, 120.0)
        self._client = client
        self.on_wait = on_wait          # called as on_wait(seconds, reason) before each retry (progress output)

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai
            from google.genai import types

            self._client = genai.Client(http_options=types.HttpOptions(
                timeout=int(self.timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),   # retries are decided here, not in the SDK
            ))
        return self._client

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            vectors.extend(self._embed(list(texts[start:start + BATCH_SIZE]), "RETRIEVAL_DOCUMENT",
                                       retries=DOCUMENT_RETRIES))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        key = (self.model, text)
        if key in _QUERY_CACHE:
            _QUERY_CACHE.move_to_end(key)
            return list(_QUERY_CACHE[key])
        vector = self._embed([text], "RETRIEVAL_QUERY", retries=0)[0]
        self._remember(text, vector)
        return list(vector)

    def warm_queries(self, texts: Sequence[str]) -> None:
        """Embed many known queries in batched, retried calls and cache them (evaluation runs).

        Later ``embed_query`` calls for these texts are answered from the cache, so an evaluation
        makes one request per 50 queries instead of one per query (kinder to per-minute limits).
        """
        todo = [t for t in dict.fromkeys(texts) if (self.model, t) not in _QUERY_CACHE]
        for start in range(0, len(todo), BATCH_SIZE):
            batch = todo[start:start + BATCH_SIZE]
            for text, vector in zip(batch, self._embed(batch, "RETRIEVAL_QUERY", retries=DOCUMENT_RETRIES),
                                    strict=True):
                self._remember(text, vector)

    def _remember(self, text: str, vector: list[float]) -> None:
        _QUERY_CACHE[(self.model, text)] = vector
        while len(_QUERY_CACHE) > QUERY_CACHE_SIZE:
            _QUERY_CACHE.popitem(last=False)

    def _embed(self, texts: list[str], task_type: str, *, retries: int) -> list[list[float]]:
        from google.genai import errors, types

        config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=EMBEDDING_DIM)
        for attempt in range(retries + 1):
            try:
                response = self._get_client().models.embed_content(model=self.model, contents=texts, config=config)
                return _vectors(response, len(texts))
            except EmbeddingError:
                raise
            except errors.APIError as exc:
                failure = _api_error(exc)
            except Exception as exc:  # httpx timeouts / connection errors raised through the SDK
                failure = _transport_error(exc)
            if attempt == retries or failure.type not in _RETRYABLE:
                raise failure
            wait = min(max(failure.retry_after or 0.0, 2.0 ** (attempt + 1)), MAX_WAIT_SECONDS)
            if self.on_wait:
                self.on_wait(wait, failure.type)
            time.sleep(wait)
        raise EmbeddingError("internal", "unreachable")  # pragma: no cover


def _vectors(response: Any, expected: int) -> list[list[float]]:
    embeddings = getattr(response, "embeddings", None) or []
    if len(embeddings) != expected:
        raise EmbeddingError("invalid_response", f"Expected {expected} embeddings, got {len(embeddings)}.")
    vectors = []
    for item in embeddings:
        values = list(getattr(item, "values", None) or [])
        if len(values) != EMBEDDING_DIM:
            raise EmbeddingError("invalid_response",
                                 f"Expected {EMBEDDING_DIM} dimensions, got {len(values)}. Is the model correct?")
        vectors.append(normalize(values))
    return vectors


def _api_error(exc: Any) -> EmbeddingError:
    code = getattr(exc, "code", None)
    detail = _API_KEY.sub("[key]", " ".join(str(getattr(exc, "message", "") or exc).split()))[:200]
    quota_ids, retry_after = _error_details(exc)
    if code == 429 and any("PerDay" in q for q in quota_ids):
        return EmbeddingError("quota_daily", "The daily Gemini embedding quota is used up "
                                             f"({', '.join(quota_ids)}). Try again tomorrow or use --keyword-only.")
    if code == 429:
        type_ = "quota"
    elif code in (401, 403):
        type_ = "auth"
    elif code == 404:
        type_ = "model_not_found"
    elif isinstance(code, int) and 400 <= code < 500:
        type_ = "invalid_request"
    else:
        type_ = "unavailable"
    return EmbeddingError(type_, f"Gemini embedding API returned {code}: {detail}",
                          retry_after=retry_after if code == 429 else None)


def _error_details(exc: Any) -> tuple[list[str], float | None]:
    """Quota IDs and the requested retry delay from a Google API error body, when present."""
    quota_ids: list[str] = []
    retry_after = None
    details = getattr(exc, "details", None)
    items = details.get("error", {}).get("details", []) if isinstance(details, dict) else []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            quota_ids += [str(v.get("quotaId")) for v in item.get("violations", []) if isinstance(v, dict)]
        elif kind.endswith("RetryInfo"):
            try:
                retry_after = float(str(item.get("retryDelay", "")).rstrip("s"))
            except ValueError:
                retry_after = None
    return quota_ids, retry_after


def _transport_error(exc: Exception) -> EmbeddingError:
    name = type(exc).__name__
    if "Timeout" in name:
        return EmbeddingError("timeout", f"Gemini embedding API did not answer in time ({name}).")
    return EmbeddingError("connection", f"Cannot reach the Gemini embedding API ({name}).")
