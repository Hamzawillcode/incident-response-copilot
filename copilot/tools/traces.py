"""Read-only Jaeger trace tool for the Incident Response Copilot.

Public API:
    get_traces(service=None, operation=None, errors_only=False, minutes=15, limit=10,
               min_duration_ms=None, trace_id=None) -> dict

Searches Jaeger 2.19 (query API under ``/jaeger/ui/api``) and returns, per
trace, its ID, duration, services, failed services, the span where the error
originated and a handful of relevant spans, plus an aggregate summary
(duration percentiles, failed services, error origins). Raw span dumps are
never returned.

Facts about the API this relies on (observed in OpenTelemetry Demo 3.1.0):
* ``GET /api/traces`` requires ``service`` (HTTP 400 otherwise); an unknown
  service returns HTTP 200 with no data.
* ``tags={"error":"true"}`` selects traces containing *any* error span. In the
  demo most baseline "error traces" contain only a failing flagd EventStream
  span, so error status is re-evaluated after sanitization.
* OTLP status is exposed as tags ``error``, ``otel.status_code`` and
  ``otel.status_description``; span events appear as ``logs`` whose ``event``
  field holds the name.
* Traces queried seconds after the request can be partial (services export
  spans in batches). Such traces are marked ``"incomplete": true`` because
  their ``error_origins`` may point too high in the call chain.

Safety: GET-only (``/api/traces`` and ``/api/traces/<id>``), proxy variables
ignored, flag-related inputs refused, every span passed through
``filters.sanitize_span`` (flagd spans and calls to flagd dropped, flag
attributes and events removed, flag text redacted) and the whole result through
``filters.scrub_output``. Query Jaeger directly (port 16686), not through the
demo's Envoy, so the copilot's own requests are not traced as shop traffic.

Configuration (environment variables)
-------------------------------------
JAEGER_URL              default ``http://localhost:16686/jaeger/ui`` (see deploy/compose.copilot.yaml)
JAEGER_TIMEOUT_SECONDS  default ``10`` (0 < t <= 60)
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter
from typing import Any

from copilot.tools import _http, filters

__all__ = ["get_traces"]

DEFAULT_JAEGER_URL = "http://localhost:16686/jaeger/ui"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_MINUTES = 7 * 24 * 60
MAX_LIMIT = 50
MAX_RELEVANT_SPANS = 5
MAX_RELEVANT_SPANS_SINGLE = 15
MAX_TEXT_CHARS = 300
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
SEARCH_PATH = "/api/traces"
_TRACE_PATH_RE = re.compile(r"^/api/traces/[0-9a-f]{16,32}$")
_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{16,32}$")

# Span attributes worth showing to an investigator (others are dropped as noise).
_USEFUL_ATTRIBUTES = (
    "http.request.method", "http.response.status_code", "http.route", "url.path", "url.full",
    "rpc.system", "rpc.service", "rpc.method", "rpc.grpc.status_code",
    "db.system", "db.system.name", "db.operation.name", "db.query.text", "db.statement",
    "messaging.system", "messaging.destination.name", "messaging.operation",
    "server.address", "server.port", "peer.service", "error.type",
)
_APP_ATTRIBUTE_PREFIXES = ("app.", "demo.")
_MAX_APP_ATTRIBUTES = 5
_INTERNAL_TAGS = frozenset({"span.kind", "error", "otel.status_code", "otel.status_description"})
_INTERNAL_TAG_PREFIXES = ("otel.scope.", "internal.")

_now = _http.now  # module-level so tests can pin the clock


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def get_traces(
    service: str | None = None,
    operation: str | None = None,
    errors_only: bool = False,
    minutes: int = 15,
    limit: int = 10,
    min_duration_ms: int | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Find distributed traces and summarise where requests fail or slow down.

    Use this to see which services fail inside a request, where an error
    originated, and how long requests take. Either search by ``service`` (with
    optional ``operation``, ``errors_only`` and ``min_duration_ms``) or fetch
    one trace by ``trace_id`` (for example a trace ID found in a log record).

    Args:
        service: Service whose traces to search, e.g. "checkout" or "frontend".
            Required unless ``trace_id`` is given.
        operation: Span name within that service, e.g.
            "oteldemo.CheckoutService/PlaceOrder".
        errors_only: Only return traces that contain at least one error span.
        minutes: Look-back window in minutes, 1 to 10080.
        limit: Maximum number of traces to return, 1 to 50.
        min_duration_ms: Only traces with a span at least this slow (milliseconds).
        trace_id: Fetch this single trace (16 or 32 hex characters); other
            filters are ignored.

    Returns:
        dict with ``status`` ``"ok"``, ``"no_data"`` or ``"error"``. On success:
        ``summary`` (counts, duration percentiles, failed services, error
        origins) and ``traces`` (per-trace summaries with relevant spans).
    """
    base: dict[str, Any] = {
        "filters": {"service": service, "operation": operation, "errors_only": errors_only,
                    "min_duration_ms": min_duration_ms, "trace_id": trace_id},
        "window_minutes": minutes,
    }
    try:
        result = _run(base, service, operation, errors_only, minutes, limit, min_duration_ms, trace_id)
    except _http.ToolError as exc:
        result = _http.error(base, exc.type, exc.message)
    except Exception as exc:  # last resort: a malformed payload must not crash the caller
        result = _http.error(base, "internal", f"Unexpected {type(exc).__name__} while processing the request.")
    return filters.scrub_output(result)


def _run(base, service, operation, errors_only, minutes, limit, min_duration_ms, trace_id) -> dict[str, Any]:
    problem = _validate(service, operation, errors_only, minutes, limit, min_duration_ms, trace_id)
    if problem:
        return _http.error(base, "invalid_input", problem)
    refusal = filters.refuse_inputs(service, operation)
    if refusal:
        return _http.error(base, "forbidden_query", refusal)

    base_url = _http.base_url_from_env("JAEGER_URL", DEFAULT_JAEGER_URL)
    timeout = _http.float_from_env("JAEGER_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 0, 60)
    allowed = lambda p: p == SEARCH_PATH or bool(_TRACE_PATH_RE.match(p))  # noqa: E731

    if trace_id:
        path = f"{SEARCH_PATH}/{trace_id.lower()}"
        status_code, body = _http.get_json(base_url=base_url, path=path, allowed=allowed, timeout=timeout,
                                           max_bytes=MAX_RESPONSE_BYTES, backend="Jaeger")
        return _interpret(base, status_code, body, limit=1, errors_only=False, single=True)

    end = _now()
    start = end - minutes * 60
    base.update(start=_http.iso(start), end=_http.iso(end))
    params: dict[str, Any] = {
        "service": service,
        "start": str(int(start * 1_000_000)),
        "end": str(int(end * 1_000_000)),
        # Sanitization can remove traces (e.g. errors that were only flagd spans): over-fetch.
        "limit": str(min(limit * 2, 2 * MAX_LIMIT)),
    }
    if operation:
        params["operation"] = operation
    if errors_only:
        params["tags"] = json.dumps({"error": "true"})
    if min_duration_ms is not None:
        params["minDuration"] = f"{min_duration_ms}ms"
    status_code, body = _http.get_json(base_url=base_url, path=SEARCH_PATH, allowed=allowed, timeout=timeout,
                                       params=params, max_bytes=MAX_RESPONSE_BYTES, backend="Jaeger")
    return _interpret(base, status_code, body, limit=limit, errors_only=errors_only, single=False)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validate(service, operation, errors_only, minutes, limit, min_duration_ms, trace_id) -> str | None:
    if trace_id is not None:
        if not isinstance(trace_id, str) or not _TRACE_ID_RE.match(trace_id):
            return "trace_id must be 16 or 32 hexadecimal characters."
        return None
    if service is None:
        return "service is required unless trace_id is given (Jaeger searches per service)."
    problem = (
        _http.validate_optional_text(service, "service", 100)
        or _http.validate_optional_text(operation, "operation", 200)
        or _http.validate_minutes(minutes, 1, MAX_MINUTES)
        or _http.validate_limit(limit, MAX_LIMIT)
    )
    if problem:
        return problem
    if not isinstance(errors_only, bool):
        return "errors_only must be true or false."
    if min_duration_ms is not None and (
        isinstance(min_duration_ms, bool) or not isinstance(min_duration_ms, int) or not 0 <= min_duration_ms <= 3_600_000
    ):
        return "min_duration_ms must be an integer between 0 and 3600000."
    return None


# --------------------------------------------------------------------------- #
# Response interpretation
# --------------------------------------------------------------------------- #
def _interpret(base: dict, status_code: int, body: Any, *, limit: int, errors_only: bool, single: bool) -> dict:
    if not isinstance(body, dict):
        return _http.error(base, "http_error",
                           f"HTTP {status_code} with a non-JSON body: {filters.redact_text(_http.snippet(body))!r}")
    errors = body.get("errors") or []
    messages = "; ".join(str(e.get("msg", e)) if isinstance(e, dict) else str(e) for e in errors)
    if status_code == 404 or (single and status_code < 400 and not body.get("data")):
        out = dict(base)
        out.update(status="no_data", traces=[], message="Trace not found (it may have expired from Jaeger's memory).")
        return out
    if status_code >= 400:
        err_type = "bad_query" if status_code == 400 else "unavailable" if status_code in (502, 503, 504) else "backend_internal" if status_code >= 500 else "http_error"
        return _http.error(base, err_type, f"HTTP {status_code}: {filters.redact_text(messages or 'error')[:MAX_TEXT_CHARS]}")
    data = body.get("data")
    if data is None:
        data = []
    if not isinstance(data, list):
        return _http.error(base, "invalid_response", "Jaeger returned an unexpected payload.")

    summaries = []
    for raw in data:
        normalised = _normalise_trace(raw)
        # Measured on the raw spans, so spans removed by sanitization do not count.
        raw_ids = {s["span_id"] for s in normalised}
        dangling = sum(1 for s in normalised if s["parent_id"] and s["parent_id"] not in raw_ids)
        spans = [s for s in (filters.sanitize_span(sp) for sp in normalised) if s is not None]
        if not spans:
            continue
        summary = _summarise_trace(spans, MAX_RELEVANT_SPANS_SINGLE if single else MAX_RELEVANT_SPANS)
        if dangling:
            # Spans reference parents Jaeger has not received yet (services export in batches):
            # the trace is still arriving, so error_origins may point too high in the call chain.
            summary["incomplete"] = True
        if errors_only and summary["error_span_count"] == 0:
            continue  # its only errors were in restricted (flag) spans
        summaries.append(summary)
    summaries.sort(key=lambda t: t["start"], reverse=True)

    out = dict(base)
    if errors:
        out["warnings"] = [filters.redact_text(messages)[:MAX_TEXT_CHARS]]
    if not summaries:
        out.update(status="no_data", traces=[], message="No matching traces in this window.")
        return out
    returned = summaries[:limit]
    out.update(status="ok", summary=_aggregate(returned), returned_traces=len(returned),
               truncated=len(summaries) > limit, traces=returned)
    return out


def _normalise_trace(raw: dict) -> list[dict[str, Any]]:
    processes = raw.get("processes") or {}
    spans = []
    for sp in raw.get("spans") or []:
        tags = {t.get("key"): t.get("value") for t in sp.get("tags") or [] if isinstance(t, dict)}
        parent = next((r.get("spanID") for r in sp.get("references") or [] if r.get("refType") == "CHILD_OF"), None)
        events = []
        for lg in sp.get("logs") or []:
            fields = {f.get("key"): f.get("value") for f in lg.get("fields") or [] if isinstance(f, dict)}
            events.append({"name": fields.pop("event", None), "attributes": fields, "timestamp_us": lg.get("timestamp")})
        spans.append({
            "trace_id": sp.get("traceID") or raw.get("traceID"),
            "span_id": sp.get("spanID"),
            "parent_id": parent,
            "service": (processes.get(sp.get("processID")) or {}).get("serviceName", "unknown"),
            "operation": sp.get("operationName", ""),
            "start_us": int(sp.get("startTime") or 0),
            "duration_us": int(sp.get("duration") or 0),
            "kind": tags.get("span.kind"),
            "error": tags.get("error") in (True, "true") or tags.get("otel.status_code") == "ERROR",
            "status_message": tags.get("otel.status_description"),
            "attributes": {k: v for k, v in tags.items()
                           if k not in _INTERNAL_TAGS and not str(k).startswith(_INTERNAL_TAG_PREFIXES)},
            "events": events,
        })
    return spans


def _summarise_trace(spans: list[dict], max_relevant: int) -> dict[str, Any]:
    by_id = {s["span_id"]: s for s in spans}
    children: dict[str, list[dict]] = {}
    for s in spans:
        if s["parent_id"] in by_id:
            children.setdefault(s["parent_id"], []).append(s)

    def has_error_below(span_id: str, seen: set) -> bool:
        for child in children.get(span_id, []):
            if child["span_id"] in seen:
                continue
            seen.add(child["span_id"])
            if child["error"] or has_error_below(child["span_id"], seen):
                return True
        return False

    error_spans = [s for s in spans if s["error"]]
    origins = [s for s in error_spans if not has_error_below(s["span_id"], set())]
    roots = [s for s in spans if s["parent_id"] not in by_id] or spans
    root = min(roots, key=lambda s: s["start_us"])
    start_us = min(s["start_us"] for s in spans)
    end_us = max(s["start_us"] + s["duration_us"] for s in spans)

    # Self time = time not covered by direct children (union of child intervals),
    # so wrappers such as the root or a proxy hop do not dominate "slowest spans".
    self_time = {s["span_id"]: _self_time_us(s, children.get(s["span_id"], [])) for s in spans}

    relevant, seen_ids = [], set()
    candidates = origins + error_spans + sorted(spans, key=lambda s: self_time[s["span_id"]], reverse=True)
    origin_ids = {s["span_id"] for s in origins}
    for s in candidates:
        if s["span_id"] in seen_ids:
            continue
        seen_ids.add(s["span_id"])
        relevant.append(_compact_span(s, s["span_id"] in origin_ids, self_time[s["span_id"]]))
        if len(relevant) >= max_relevant:
            break

    return {
        "trace_id": root["trace_id"],
        "start": _http.iso(start_us / 1_000_000),
        "duration_ms": round((end_us - start_us) / 1000, 3),
        "root_service": root["service"],
        "root_operation": root["operation"],
        "span_count": len(spans),
        "services": sorted({s["service"] for s in spans}),
        "error_span_count": len(error_spans),
        "failed_services": sorted({s["service"] for s in error_spans}),
        "error_origins": [{"service": s["service"], "operation": s["operation"]} for s in origins],
        "relevant_spans": relevant,
    }


def _self_time_us(span: dict, kids: list[dict]) -> int:
    """Span duration minus the union of its direct children's intervals (clipped to the span)."""
    lo, hi = span["start_us"], span["start_us"] + span["duration_us"]
    intervals = sorted((max(lo, c["start_us"]), min(hi, c["start_us"] + c["duration_us"])) for c in kids)
    covered, cur_start, cur_end = 0, None, None
    for a, b in intervals:
        if b <= a:
            continue
        if cur_end is None or a > cur_end:
            if cur_end is not None:
                covered += cur_end - cur_start
            cur_start, cur_end = a, b
        else:
            cur_end = max(cur_end, b)
    if cur_end is not None:
        covered += cur_end - cur_start
    return max(0, span["duration_us"] - covered)


def _compact_span(s: dict, is_origin: bool, self_time_us: int | None = None) -> dict[str, Any]:
    attrs = {k: s["attributes"][k] for k in _USEFUL_ATTRIBUTES if k in s["attributes"]}
    app = [(k, v) for k, v in s["attributes"].items() if str(k).startswith(_APP_ATTRIBUTE_PREFIXES)]
    attrs.update(dict(app[:_MAX_APP_ATTRIBUTES]))
    attrs = {k: (_truncate(v) if isinstance(v, str) else v) for k, v in attrs.items()}
    out: dict[str, Any] = {
        "service": s["service"],
        "operation": s["operation"],
        "kind": s["kind"],
        "duration_ms": round(s["duration_us"] / 1000, 3),
        "error": s["error"],
    }
    if self_time_us is not None:
        out["self_time_ms"] = round(self_time_us / 1000, 3)
    if is_origin:
        out["error_origin"] = True
    if s["status_message"]:
        out["status_message"] = _truncate(str(s["status_message"]))
    exceptions = [e for e in s["events"] if e.get("name") == "exception"]
    if exceptions:
        ea = exceptions[0]["attributes"]
        out["exception"] = {k.split(".", 1)[1]: _truncate(str(ea[k])) for k in ("exception.type", "exception.message")
                            if ea.get(k) is not None}
    if attrs:
        out["attributes"] = attrs
    return out


def _aggregate(traces: list[dict]) -> dict[str, Any]:
    durations = sorted(t["duration_ms"] for t in traces)
    failed = Counter(svc for t in traces for svc in t["failed_services"])
    origins = Counter(f'{o["service"]} :: {o["operation"]}' for t in traces for o in t["error_origins"])
    roots = Counter(f'{t["root_service"]} :: {t["root_operation"]}' for t in traces)
    return {
        "trace_count": len(traces),
        "error_trace_count": sum(1 for t in traces if t["error_span_count"]),
        "incomplete_trace_count": sum(1 for t in traces if t.get("incomplete")),
        "duration_ms": {
            "min": durations[0],
            "p50": _percentile(durations, 50),
            "p95": _percentile(durations, 95),
            "max": durations[-1],
            "mean": round(statistics.fmean(durations), 3),
        },
        "failed_services": dict(failed.most_common()),
        "error_origins": dict(origins.most_common(10)),
        "root_operations": dict(roots.most_common(5)),
    }


def _percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile (no interpolation; honest for small samples)."""
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _truncate(s: str, n: int = MAX_TEXT_CHARS) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
