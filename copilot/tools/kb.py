"""Knowledge-base search tools (RAG): runbooks and postmortems.

Public API:
    search_runbooks(query: str, k: int = 4) -> dict
    search_postmortems(query: str, k: int = 3) -> dict

Same contract as the telemetry tools: simple parameters, a docstring written for
the LLM, a dict with ``status`` ``"ok"`` | ``"no_data"`` | ``"error"``, never raises,
and the whole result passes through ``filters.scrub_output``. Queries that mention
fault-injection terms are refused before any search runs.

Read-only: searches use a read-only database session (``copilot.kb.store``). The
query text may be sent to the Gemini embedding API for semantic search. If that is
not possible, the search falls back to keyword matching and says so in ``warnings``.

How retrieval works (keyword + vector search, fused with RRF and grouped by
document) is described in ``copilot.kb.search``.
"""

from __future__ import annotations

from typing import Any

from copilot.tools import _http, filters

# copilot.kb is imported inside _search(): copilot.kb itself imports copilot.tools (filters,
# env helpers), and importing it here, while copilot.tools/__init__ runs, would be circular.

__all__ = ["search_postmortems", "search_runbooks"]

MAX_QUERY_CHARS = 500
MAX_K = 8
NOTE = ("Background knowledge written before this incident. It is not evidence about the current incident: "
        "confirm any idea from it with metrics, traces or logs.")


def search_runbooks(query: str, k: int = 4) -> dict[str, Any]:
    """Search the team's runbooks: how to investigate and fix a kind of problem.

    Describe the symptoms or the question in plain words, for example
    "checkout errors, payment charge failing" or "recommendation latency rising,
    memory growing". Returns the best-matching runbooks, each with its most
    relevant sections. Use them to decide what to check next. A runbook is general
    guidance, not evidence about the current incident.

    Args:
        query: Symptoms or question in plain words, up to 500 characters.
        k: Number of runbooks to return, 1 to 8.

    Returns:
        dict with ``status`` ``"ok"``, ``"no_data"`` or ``"error"``. On success:
        ``retrieval`` (``"hybrid"`` or ``"keyword"``) and ``results``, each with
        ``source``, ``title``, ``services``, ``score``, ``matched_by`` and
        ``sections`` (``heading``, ``text``).
    """
    return _search("runbook", query, k)


def search_postmortems(query: str, k: int = 3) -> dict[str, Any]:
    """Search postmortems of past incidents for ones with similar symptoms.

    Describe the current symptoms in plain words, for example "checkout failing,
    errors start in the payment charge call". Each result is a past incident with
    its root cause, failure type and lessons. A similar past incident is a
    hypothesis to check with telemetry, not an answer. Its times, numbers and IDs
    are about the past, not about the current incident.

    Args:
        query: Current symptoms in plain words, up to 500 characters.
        k: Number of postmortems to return, 1 to 8.

    Returns:
        dict with ``status`` ``"ok"``, ``"no_data"`` or ``"error"``. On success:
        ``retrieval`` and ``results``, each with ``source``, ``title``,
        ``incident_id``, ``date``, ``root_cause_service``, ``failure_type``,
        ``services``, ``score`` and ``sections`` (``heading``, ``text``).
    """
    return _search("postmortem", query, k)


def _search(kind: str, query: Any, k: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": kind,
        "query": query[:200] if isinstance(query, str) else None,
        "k": k if isinstance(k, int) and not isinstance(k, bool) else None,
    }
    try:
        from copilot.kb import search as kb_search
        from copilot.kb.store import StoreError

        problem = _validate(query, k)
        if problem:
            result = _http.error(base, "invalid_input", problem)
        elif refusal := filters.refuse_inputs(query):
            result = _http.error(base, "forbidden_query", refusal)
        else:
            try:
                result = {**base, **kb_search.search(kind, " ".join(query.split()), k)}
            except (StoreError, kb_search.SearchError) as exc:
                result = _http.error(base, exc.type, exc.message)
            if result.get("status") == "ok":
                result["note"] = NOTE
    except Exception as exc:  # last resort: the tool contract is "never raise"
        result = _http.error(base, "internal", f"Unexpected {type(exc).__name__} while searching the knowledge base.")
    return filters.scrub_output(result)


def _validate(query: Any, k: Any) -> str | None:
    if not isinstance(query, str) or not query.strip():
        return "query must be a non-empty string."
    if len(query) > MAX_QUERY_CHARS:
        return f"query is longer than {MAX_QUERY_CHARS} characters; describe the symptoms more briefly."
    if "\x00" in query:
        return "query contains a NUL character."
    return _http.validate_limit(k, MAX_K, name="k")
