"""Knowledge base (Week 3): runbooks and postmortems in Postgres + pgvector, searched with RAG.

    documents.py   read kb/*.md, validate, split into one chunk per "##" section
    embeddings.py  Gemini embeddings (RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY, 768 dims)
    store.py       database connection (read-only for searches) and the two search queries
    search.py      hybrid retrieval: keyword + vector candidates fused with RRF, grouped by document
    ingest.py      python -m copilot.kb.ingest  (only changed sections are re-embedded)

The agent-facing tools are ``copilot.tools.kb.search_runbooks`` and ``search_postmortems``.
"""
