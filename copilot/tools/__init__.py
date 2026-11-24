"""Read-only observability tools for the Incident Response Copilot.

Each tool is a plain Python function with simple, JSON-friendly parameters and
a docstring written for an LLM, so it can later be registered with an agent
framework (e.g. Google ADK) as-is. Every tool:

* is read-only (GET requests to an allow-listed path only),
* returns a summarised dict with ``status`` ``"ok"`` | ``"no_data"`` | ``"error"``,
* never raises,
* passes its result through ``filters.scrub_output`` so no feature-flag
  (fault-injection) information can reach the caller.
"""

from copilot.tools.health import get_service_health, get_service_metrics
from copilot.tools.logs import search_logs
from copilot.tools.metrics import query_metrics
from copilot.tools.traces import get_traces

#: The read-only investigation toolset, in the order an investigation usually uses them.
TOOLS = (get_service_health, get_service_metrics, get_traces, search_logs, query_metrics)

__all__ = ["TOOLS", "get_service_health", "get_service_metrics", "get_traces", "query_metrics", "search_logs"]
