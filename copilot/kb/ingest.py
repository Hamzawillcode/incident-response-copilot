"""Load the kb/ documents into Postgres + pgvector.

    python -m copilot.kb.ingest                  # incremental: embeds only new or changed sections
    python -m copilot.kb.ingest --dry-run        # parse and validate only, no database
    python -m copilot.kb.ingest --keyword-only   # store text without embeddings
    python -m copilot.kb.ingest --rebuild        # drop and recreate the table first

Steps
-----
1. Read and validate every document (``copilot.kb.documents``). Documents that
   mention how faults are injected are refused, so nothing is written.
2. Split them into section chunks, each with a content hash.
3. Compare with the stored rows: a chunk is (re-)embedded only if it is new, its
   text changed, or it was embedded with a different model. Re-running ingestion on
   an unchanged knowledge base makes no embedding calls at all.
4. Embed what is needed. This happens **before** any write, so a quota error or a
   network failure leaves the database exactly as it was.
5. One transaction: upsert changed chunks, store new embeddings, delete chunks whose
   section no longer exists.

Without a Gemini key, step 4 is skipped: text is stored and searches run keyword-only.
Run ingestion again after adding the key to fill in the embeddings.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from copilot.kb import documents, embeddings, store
from copilot.kb.documents import Chunk, DocumentError
from copilot.kb.embeddings import EMBEDDING_DIM, Embedder, EmbeddingError
from copilot.kb.store import TABLE, StoreError, qualified_table, translate_errors

__all__ = ["DEFAULT_KB_DIR", "IngestReport", "ensure_schema", "ingest", "main"]

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KB_DIR = ROOT / "kb"

# {table} = <schema>.kb_chunks and {dim} = EMBEDDING_DIM, filled in at run time (see store.qualified_table).
SCHEMA_STATEMENTS = (
    "CREATE EXTENSION IF NOT EXISTS vector SCHEMA public",
    """
    CREATE TABLE IF NOT EXISTS {table} (
        id              text PRIMARY KEY,
        kind            text NOT NULL CHECK (kind IN ('runbook', 'postmortem')),
        doc_id          text NOT NULL,
        source          text NOT NULL,
        title           text NOT NULL,
        heading         text NOT NULL,
        position        integer NOT NULL,
        text            text NOT NULL,
        keywords        text NOT NULL DEFAULT '',
        metadata        jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        content_hash    text NOT NULL,
        embedding       vector({dim}),
        embedding_model text,
        tsv             tsvector GENERATED ALWAYS AS (
                            setweight(to_tsvector('english', title || ' ' || heading), 'A') ||
                            setweight(to_tsvector('english', keywords), 'B') ||
                            setweight(to_tsvector('english', text), 'D')) STORED,
        updated_at      timestamptz NOT NULL DEFAULT now(),
        CHECK ((embedding IS NULL) = (embedding_model IS NULL))
    )""",
    f"CREATE INDEX IF NOT EXISTS {TABLE}_tsv_idx ON {{table}} USING gin (tsv)",
    f"CREATE INDEX IF NOT EXISTS {TABLE}_kind_idx ON {{table}} (kind)",
)

_UPSERT_SQL = """
INSERT INTO {table} (id, kind, doc_id, source, title, heading, position, text, keywords, metadata,
                     content_hash, embedding, embedding_model, updated_at)
VALUES (%(id)s, %(kind)s, %(doc_id)s, %(source)s, %(title)s, %(heading)s, %(position)s, %(text)s, %(keywords)s,
        %(metadata)s, %(content_hash)s, %(embedding)s::vector, %(embedding_model)s, now())
ON CONFLICT (id) DO UPDATE SET
    kind = EXCLUDED.kind, doc_id = EXCLUDED.doc_id, source = EXCLUDED.source, title = EXCLUDED.title,
    heading = EXCLUDED.heading, position = EXCLUDED.position, text = EXCLUDED.text, keywords = EXCLUDED.keywords,
    metadata = EXCLUDED.metadata, content_hash = EXCLUDED.content_hash, embedding = EXCLUDED.embedding,
    embedding_model = EXCLUDED.embedding_model, updated_at = now()
"""
_SET_EMBEDDING_SQL = """
UPDATE {table} SET embedding = %(embedding)s::vector, embedding_model = %(embedding_model)s, updated_at = now()
WHERE id = %(id)s
"""
_DELETE_SQL = "DELETE FROM {table} WHERE id = ANY(%(ids)s)"
_STORED_SQL = "SELECT id, content_hash, embedding_model FROM {table}"
_SUMMARY_SQL = """
SELECT kind, count(*) AS chunks, count(embedding) AS embedded, count(DISTINCT source) AS documents
FROM {table} GROUP BY kind ORDER BY kind
"""


@dataclass
class IngestReport:
    documents: int = 0
    chunks: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    embedded: int = 0
    embedding_model: str | None = None
    dry_run: bool = False
    stored: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sql(template: str) -> str:
    return template.format(table=qualified_table(), dim=EMBEDDING_DIM)


def ensure_schema(conn) -> None:
    with translate_errors():
        for statement in SCHEMA_STATEMENTS:
            conn.execute(_sql(statement))


def ingest(kb_dir: str | Path = DEFAULT_KB_DIR, *, embedder: Embedder | None = None, rebuild: bool = False,
           dry_run: bool = False) -> IngestReport:
    """Synchronise the database with ``kb_dir``. ``embedder=None`` stores text only.

    Raises ``DocumentError`` (bad document), ``EmbeddingError`` (nothing written) or ``StoreError``.
    """
    chunks = documents.load_chunks(kb_dir)
    report = IngestReport(documents=len({c.source for c in chunks}), chunks=len(chunks),
                          embedding_model=embedder.model if embedder else None, dry_run=dry_run)
    if dry_run:
        return report

    with store.connect(read_only=False) as conn:
        with translate_errors(), conn.transaction():
            if rebuild:
                conn.execute(_sql("DROP TABLE IF EXISTS {table}"))
            ensure_schema(conn)
        with translate_errors():
            stored = {row[0]: (row[1], row[2]) for row in conn.execute(_sql(_STORED_SQL)).fetchall()}

        changed = [c for c in chunks if stored.get(c.id, (None, None))[0] != c.content_hash]
        report.inserted = sum(1 for c in changed if c.id not in stored)
        report.updated = len(changed) - report.inserted
        report.unchanged = len(chunks) - len(changed)
        stale = [c for c in chunks if c.id in stored and stored[c.id][0] == c.content_hash
                 and embedder is not None and stored[c.id][1] != embedder.model]
        to_embed = (changed + stale) if embedder is not None else []

        vectors: dict[str, str] = {}
        if to_embed:  # before any write: a failure here leaves the database untouched
            for chunk, vector in zip(to_embed, embedder.embed_documents([c.embedding_text for c in to_embed]),
                                     strict=True):
                vectors[chunk.id] = embeddings.to_pgvector(vector)
        report.embedded = len(vectors)

        removed = sorted(set(stored) - {c.id for c in chunks})
        with translate_errors(), conn.transaction():
            for chunk in changed:
                conn.execute(_sql(_UPSERT_SQL), _row(chunk, vectors.get(chunk.id), embedder))
            for chunk in stale:
                conn.execute(_sql(_SET_EMBEDDING_SQL), {"id": chunk.id, "embedding": vectors[chunk.id],
                                                  "embedding_model": embedder.model})
            if removed:
                conn.execute(_sql(_DELETE_SQL), {"ids": removed})
        report.deleted = len(removed)
        with translate_errors():
            report.stored = {row[0]: {"documents": row[3], "chunks": row[1], "embedded": row[2]}
                             for row in conn.execute(_sql(_SUMMARY_SQL)).fetchall()}
    return report


def _row(chunk: Chunk, vector: str | None, embedder: Embedder | None) -> dict[str, Any]:
    return {
        "id": chunk.id, "kind": chunk.kind, "doc_id": chunk.doc_id, "source": chunk.source, "title": chunk.title,
        "heading": chunk.heading, "position": chunk.position, "text": chunk.text, "keywords": chunk.keywords,
        "metadata": Jsonb(chunk.metadata), "content_hash": chunk.content_hash, "embedding": vector,
        "embedding_model": embedder.model if (vector is not None and embedder is not None) else None,
    }


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m copilot.kb.ingest", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kb-dir", default=str(DEFAULT_KB_DIR), help="knowledge-base folder (default: kb/)")
    parser.add_argument("--dry-run", action="store_true", help="parse and validate only")
    parser.add_argument("--keyword-only", action="store_true", help="do not call the embedding API")
    parser.add_argument("--rebuild", action="store_true", help="drop and recreate the table first")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    try:
        embedder = None if (args.keyword_only or args.dry_run) else embeddings.make_embedder()
        if embedder is None and not (args.keyword_only or args.dry_run):
            print("note: no Gemini key configured (GOOGLE_API_KEY in .env), so sections are stored without "
                  "embeddings and searches run keyword-only. Run ingestion again after adding the key.",
                  file=sys.stderr)
        report = ingest(args.kb_dir, embedder=embedder, rebuild=args.rebuild, dry_run=args.dry_run)
    except DocumentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except EmbeddingError as exc:
        print(f"error: embedding failed ({exc.type}): {exc.message}\nNothing was written. Retry later, or use "
              "--keyword-only.", file=sys.stderr)
        return 3
    except StoreError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 4

    _print_report(report)
    return 0


def _print_report(report: IngestReport) -> None:
    if report.dry_run:
        print(f"OK (dry run): {report.documents} documents, {report.chunks} sections. Nothing written.")
        return
    model = report.embedding_model or "none (keyword-only)"
    print(f"Ingested {report.documents} documents, {report.chunks} sections. Embedding model: {model}")
    print(f"  inserted {report.inserted}, updated {report.updated}, unchanged {report.unchanged}, "
          f"deleted {report.deleted}, embedded {report.embedded}")
    for kind, row in report.stored.items():
        print(f"  stored {kind}s: {row['documents']} documents, {row['chunks']} sections, "
              f"{row['embedded']} with embeddings")


if __name__ == "__main__":
    raise SystemExit(main())
