"""Tracing the copilot itself (Week 4): OpenTelemetry spans for every incident, step, LLM and tool call.

    setup_tracing()       # once, at program start; no-op unless COPILOT_OTLP_ENDPOINT is set

ADK already creates spans for agent runs, LLM calls and tool calls (with model, token counts
and tool names). The coordinator adds an ``incident`` span and one ``step <agent>`` span per
agent step, so one incident is one trace: incident -> step triage -> call_llm / execute_tool ...

Where the spans go
------------------
To the copilot's **own** Jaeger (``copilot-jaeger`` in docker-compose.yml), via OTLP/HTTP.
Never to the demo's collector: its span-metrics pipeline would turn the copilot's own
traffic into a service called "incident-copilot" in Prometheus, and the agents would then
see their own LLM latency in ``get_service_health``. ``setup_tracing`` refuses the demo
collector's well-known ports for that reason.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

__all__ = ["SERVICE_NAME", "setup_tracing"]

SERVICE_NAME = "incident-copilot"
_DEMO_COLLECTOR_PORTS = {4317, 4318}
_configured = False


def setup_tracing(endpoint: str | None = None) -> str | None:
    """Export spans to ``endpoint`` (default ``$COPILOT_OTLP_ENDPOINT``). Returns the endpoint used or None.

    Example endpoint: ``http://localhost:4328/v1/traces`` (the copilot-jaeger container).
    """
    global _configured
    endpoint = (endpoint or os.environ.get("COPILOT_OTLP_ENDPOINT", "")).strip()
    if not endpoint or _configured:
        return endpoint if _configured else None
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("COPILOT_OTLP_ENDPOINT must be an http(s) URL such as http://localhost:4328/v1/traces.")
    if parts.port in _DEMO_COLLECTOR_PORTS:
        raise ValueError("COPILOT_OTLP_ENDPOINT points at an OpenTelemetry Collector port (4317/4318). Send the "
                         "copilot's spans to copilot-jaeger (port 4328), never into the demo's own telemetry.")

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5)))
    trace.set_tracer_provider(provider)
    _configured = True
    return endpoint


def shutdown_tracing() -> None:
    """Flush buffered spans before the program exits (otherwise the last batch is lost)."""
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        shutdown()
