"""Read-only tools for the Incident Response Copilot.

Each tool is a plain Python function with simple, JSON-friendly parameters and
a docstring written for an LLM, so it can be registered with an agent framework
(Google ADK) as-is. Every tool:

* is read-only (GET requests to an allow-listed path, or a read-only database session),
* returns a summarised dict with ``status`` ``"ok"`` | ``"no_data"`` | ``"error"``,
* never raises,
* passes its result through ``filters.scrub_output`` so no feature-flag
  (fault-injection) information can reach the caller.
"""

from copilot.tools.health import get_service_health, get_service_metrics
from copilot.tools.kb import search_postmortems, search_runbooks
from copilot.tools.logs import search_logs
from copilot.tools.metrics import query_metrics
from copilot.tools.traces import get_traces

#: The read-only telemetry toolset, in the order an investigation usually uses them.
TOOLS = (get_service_health, get_service_metrics, get_traces, search_logs, query_metrics)

#: Knowledge-base (RAG) tools: runbooks and past incidents. Week 3.
KB_TOOLS = (search_runbooks, search_postmortems)

__all__ = ["KB_TOOLS", "TOOLS", "get_service_health", "get_service_metrics", "get_traces", "query_metrics",
           "search_logs", "search_postmortems", "search_runbooks"]
