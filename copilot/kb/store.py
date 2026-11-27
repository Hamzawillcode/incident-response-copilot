"""Postgres + pgvector access for the knowledge base: connection and the two search queries.

Table ``kb_chunks`` (created by ``copilot.kb.ingest``) holds one row per document
section: the text, its metadata, a generated full-text column ``tsv`` and a 768-dim
``embedding`` (NULL until a Gemini key is configured).

Read-only by construction
-------------------------
Searches connect with ``default_transaction_read_only=on`` and a statement timeout,
and this module contains only SELECT statements (a test checks this). The only
writer is ``copilot.kb.ingest``. All values travel as query parameters, never as
SQL text.

Keyword query
-------------
``plainto_tsquery`` turns the user's words into lexemes (stemmed, stop words
removed, no operator syntax, so no injection). Its ANDs are rewritten to ORs: a
long description of symptoms should rank documents by how many words they share,
not match only documents containing every word. ``ts_rank`` weighs title and heading
words (weight A) above front-matter keywords (B) and body text (D).

Vector query
------------
Exact cosine distance (``<=>``) over the rows embedded with the *same* model as the
query: vectors from different models are not comparable. With a few hundred rows an
exact scan takes about a millisecond; an approximate (HNSW) index only pays off from
tens of thousands of rows and returns fewer rows when combined with WHERE filters.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from copilot.tools._http import int_from_env

__all__ = ["Candidate", "DEFAULT_DATABASE_URL", "StoreError", "TABLE", "chunk_counts", "connect", "database_url",
           "keyword_candidates", "qualified_table", "safe_dsn", "schema_name", "translate_errors",
           "vector_candidates"]

DEFAULT_DATABASE_URL = "postgresql://copilot:copilot@localhost:5433/copilot"
TABLE = "kb_chunks"
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_SELECT_COLUMNS = "id, kind, doc_id, source, title, heading, position, text, metadata"


class StoreError(Exception):
    """A database problem. ``type``: configuration | connection | timeout | not_initialized | database."""

    def __init__(self, type_: str, message: str):
        super().__init__(message)
        self.type = type_
        self.message = message


@dataclass(frozen=True)
class Candidate:
    """One chunk returned by a search query, with that query's own score."""

    id: str
    kind: str
    doc_id: str
    source: str
    title: str
    heading: str
    position: int
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


# --------------------------------------------------------------------------- #
# Configuration and connection
# --------------------------------------------------------------------------- #
def database_url() -> str:
    url = os.environ.get("KB_DATABASE_URL", "").strip() or DEFAULT_DATABASE_URL
    if urlsplit(url).scheme not in ("postgresql", "postgres"):
        raise StoreError("configuration", "KB_DATABASE_URL must be a postgresql:// URL.")
    return url


def safe_dsn(url: str) -> str:
    """The URL without its password, for messages."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    netloc = parts.netloc.rsplit("@", 1)
    user = (parts.username or "") + ":***"
    return parts._replace(netloc=f"{user}@{netloc[1]}").geturl()


def schema_name() -> str:
    schema = os.environ.get("KB_SCHEMA", "").strip() or "public"
    if not _IDENTIFIER.fullmatch(schema):
        raise StoreError("configuration", "KB_SCHEMA must be a lowercase SQL identifier.")
    return schema


def qualified_table() -> str:
    """``<schema>.kb_chunks``. Every statement names the schema explicitly: with an unqualified name,
    a missing table in KB_SCHEMA would silently resolve to ``public.kb_chunks`` through search_path."""
    return f"{schema_name()}.{TABLE}"


def connect(*, read_only: bool = True) -> psycopg.Connection:
    """Open a connection to the knowledge base. Tests replace this function.

    ``read_only=True`` (every search) makes the whole session read-only on the server side.
    """
    url = database_url()
    timeout_ms = int_from_env("KB_STATEMENT_TIMEOUT_MS", 5000, 100, 600_000)
    options = f"-c statement_timeout={timeout_ms} -c search_path={schema_name()},public"
    if read_only:
        options += " -c default_transaction_read_only=on"
    try:
        # autocommit: searches are single statements; ingestion wraps its writes in explicit transactions.
        return psycopg.connect(url, connect_timeout=int_from_env("KB_CONNECT_TIMEOUT_SECONDS", 3, 1, 60),
                               options=options, application_name="incident-copilot-kb", autocommit=True)
    except psycopg.ProgrammingError:
        raise StoreError("configuration", "KB_DATABASE_URL is not a valid connection URL.") from None
    except psycopg.Error:
        raise StoreError("connection", f"Cannot reach the knowledge base at {safe_dsn(url)}. "
                                       "Start it with `docker compose up -d kb-postgres`.") from None


@contextmanager
def translate_errors() -> Iterator[None]:
    """Turn psycopg exceptions into :class:`StoreError` with a stable type and a safe message."""
    try:
        yield
    except StoreError:
        raise
    except (pg_errors.UndefinedTable, pg_errors.UndefinedObject, pg_errors.UndefinedFunction):
        raise StoreError("not_initialized", "The knowledge base is not set up yet. "
                                            "Run `python -m copilot.kb.ingest`.") from None
    except pg_errors.QueryCanceled:
        raise StoreError("timeout", "The knowledge-base query took too long and was cancelled.") from None
    except psycopg.OperationalError:
        raise StoreError("connection", "Lost the connection to the knowledge base.") from None
    except psycopg.Error as exc:
        raise StoreError("database", f"Knowledge-base query failed ({type(exc).__name__}).") from None


# --------------------------------------------------------------------------- #
# Queries (SELECT only). {table} is filled with qualified_table() at call time.
# --------------------------------------------------------------------------- #
_KEYWORD_SQL = f"""
SELECT {_SELECT_COLUMNS}, ts_rank(tsv, query.q) AS score
FROM {{table}},
     (SELECT replace(plainto_tsquery('english', %(query)s)::text, ' & ', ' | ')::tsquery AS q) AS query
WHERE kind = %(kind)s AND tsv @@ query.q
ORDER BY score DESC, id
LIMIT %(limit)s
"""

_VECTOR_SQL = f"""
SELECT {_SELECT_COLUMNS}, 1 - (embedding <=> %(vector)s::vector) AS score
FROM {{table}}
WHERE kind = %(kind)s AND embedding IS NOT NULL AND embedding_model = %(model)s
ORDER BY embedding <=> %(vector)s::vector, id
LIMIT %(limit)s
"""

_COUNTS_SQL = f"""
SELECT kind, count(*) AS chunks,
       count(*) FILTER (WHERE embedding IS NOT NULL AND embedding_model = %(model)s) AS embedded
FROM {{table}}
GROUP BY kind
"""


def keyword_candidates(conn: psycopg.Connection, kind: str, query: str, limit: int) -> list[Candidate]:
    """Full-text matches for ``query`` (any word), best first."""
    with translate_errors(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_KEYWORD_SQL.format(table=qualified_table()), {"query": query, "kind": kind, "limit": limit})
        return [_candidate(row) for row in cur.fetchall()]


def vector_candidates(conn: psycopg.Connection, kind: str, vector: str, model: str, limit: int) -> list[Candidate]:
    """Nearest chunks to ``vector`` (pgvector text form) among rows embedded with ``model``."""
    with translate_errors(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_VECTOR_SQL.format(table=qualified_table()),
                    {"vector": vector, "kind": kind, "model": model, "limit": limit})
        return [_candidate(row) for row in cur.fetchall()]


def chunk_counts(conn: psycopg.Connection, model: str | None) -> dict[str, dict[str, int]]:
    """{kind: {"chunks": n, "embedded": m}}, where m counts rows embedded with ``model``."""
    with translate_errors(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_COUNTS_SQL.format(table=qualified_table()), {"model": model or ""})
        return {row["kind"]: {"chunks": int(row["chunks"]), "embedded": int(row["embedded"])}
                for row in cur.fetchall()}


def _candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(id=row["id"], kind=row["kind"], doc_id=row["doc_id"], source=row["source"],
                     title=row["title"], heading=row["heading"], position=int(row["position"]), text=row["text"],
                     score=float(row["score"]), metadata=dict(row.get("metadata") or {}))
