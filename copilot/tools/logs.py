"""Read-only OpenSearch log search tool for the Incident Response Copilot.

Public API:
    search_logs(service=None, level=None, text=None, minutes=15, limit=20, trace_id=None) -> dict

Queries the demo's log indices (``otel-logs-*``, written by the Collector's
``opensearch`` exporter) and returns a summary instead of a log dump: counts by
service and level, recurring message patterns, and a few recent records.

Facts about the data this relies on (observed in OpenTelemetry Demo 3.1.0):
* ``@timestamp`` is ``1970-01-01`` for services whose SDK does not set a log
  timestamp (shipping/Rust, currency/C++), so time filtering and ordering use
  ``observedTimestamp``, which the Collector always sets.
* Severity is inconsistent (``INFO``/``info``/``Information``; some records have
  only text, ~40 % have none). Level filters match ``severity.number`` *or* a
  normalised ``severity.text``.
* ``_source`` stores resource attributes with dotted keys
  (``resource["service.name"]``).

Safety: GET-only (``GET /otel-logs-*/_search`` with a JSON body), proxy
variables ignored, flag-related records excluded inside the query (so counts and
aggregations are clean) and again after retrieval (``filters.sanitize_log``),
flag-related inputs refused, and the result passes through
``filters.scrub_output``.

Configuration (environment variables)
-------------------------------------
OPENSEARCH_URL              default ``http://localhost:9200`` (see deploy/compose.copilot.yaml)
OPENSEARCH_LOGS_INDEX       default ``otel-logs-*``
OPENSEARCH_TIMEOUT_SECONDS  default ``10`` (0 < t <= 60)
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from copilot.tools import _http, filters

__all__ = ["search_logs"]

DEFAULT_OPENSEARCH_URL = "http://localhost:9200"
DEFAULT_INDEX = "otel-logs-*"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_MINUTES = 7 * 24 * 60
MAX_LIMIT = 100
SAMPLE_SIZE = 200  # records fetched to build message patterns
MAX_PATTERNS = 10
MAX_MESSAGE_CHARS = 300
MAX_ATTRIBUTES = 8
MAX_ATTRIBUTE_CHARS = 120
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_INDEX_RE = re.compile(r"^[a-z0-9][a-z0-9*_.\-]*$")
_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# OpenTelemetry severity numbers: TRACE 1-4, DEBUG 5-8, INFO 9-12, WARN 13-16, ERROR 17-20, FATAL 21-24.
LEVELS: "OrderedDict[str, tuple[int, int, tuple[str, ...]]]" = OrderedDict(
    trace=(1, 4, ("trace",)),
    debug=(5, 8, ("debug",)),
    info=(9, 12, ("info", "information", "informational")),
    warn=(13, 16, ("warn", "warning")),
    error=(17, 20, ("error", "err")),
    fatal=(21, 24, ("fatal", "critical", "crit", "panic", "emergency")),
)
_LEVEL_ALIASES = {"warning": "warn", "information": "info", "critical": "fatal"}

_SERVER_SIDE_EXCLUSION = (
    "*flagd* OR *flagservice* OR *ofrep* OR *openfeature* OR *featureflag* OR *feature_flag* "
    'OR "feature flag" OR ' + " OR ".join(filters.FLAG_KEYS)
)
_NOISY_ATTRIBUTE_PREFIXES = ("data_stream", "otel", "code.", "service.name", "resource ", "log records", "trace", "span")

# Message templating: collapse values that differ between otherwise identical messages.
_TEMPLATE_RULES = [
    (re.compile(r"^\[\d{4}-\d{2}-\d{2}T[^\]]*\]\s*"), ""),  # Envoy access-log timestamp prefix
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?\b"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"\b[A-Z0-9]{10}\b"), "<id>"),  # product ids such as 1YMWWN1N4O
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]

_now = _http.now  # module-level so tests can pin the clock


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def search_logs(
    service: str | None = None,
    level: str | None = None,
    text: str | None = None,
    minutes: int = 15,
    limit: int = 20,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Search application logs and summarise what they say.

    Use this to find error or warning messages, see which services are logging
    problems, or read the logs belonging to one trace. Results are grouped into
    recurring message patterns with counts, plus the most recent matching
    records.

    Args:
        service: Only logs from this service, e.g. "checkout" or "frontend-proxy".
        level: Minimum severity: "trace", "debug", "info", "warn", "error" or
            "fatal". "warn" returns warnings, errors and fatals. Records without a
            severity are excluded when a level is given.
        text: Words that must all appear in the log message, e.g. "payment failed".
        minutes: Look-back window in minutes, 1 to 10080.
        limit: Number of recent records to include, 1 to 100.
        trace_id: Only logs carrying this 32-hex-character trace ID.

    Returns:
        dict with ``status`` ``"ok"``, ``"no_data"`` or ``"error"``. On success:
        ``total_matches``, ``by_service``, ``by_level``, ``patterns`` and
        ``records``.
    """
    base: dict[str, Any] = {
        "filters": {"service": service, "level": level, "text": text, "trace_id": trace_id},
        "window_minutes": minutes,
    }
    try:
        result = _search(base, service, level, text, minutes, limit, trace_id)
    except _http.ToolError as exc:
        result = _http.error(base, exc.type, exc.message)
    except Exception as exc:  # last resort: a malformed payload must not crash the caller
        result = _http.error(base, "internal", f"Unexpected {type(exc).__name__} while processing the request.")
    return filters.scrub_output(result)


def _search(base, service, level, text, minutes, limit, trace_id) -> dict[str, Any]:
    problem = _validate(service, level, text, minutes, limit, trace_id)
    if problem:
        return _http.error(base, "invalid_input", problem)
    refusal = filters.refuse_inputs(service, text)
    if refusal:
        return _http.error(base, "forbidden_query", refusal)

    base_url = _http.base_url_from_env("OPENSEARCH_URL", DEFAULT_OPENSEARCH_URL)
    index = os.environ.get("OPENSEARCH_LOGS_INDEX", DEFAULT_INDEX)
    if not _INDEX_RE.match(index):
        return _http.error(base, "configuration", f"OPENSEARCH_LOGS_INDEX {index!r} is not a valid index pattern.")
    timeout = _http.float_from_env("OPENSEARCH_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 0, 60)
    level_key = _normalise_level(level) if level else None

    end = _now()
    start = end - minutes * 60
    base.update(start=_http.iso(start), end=_http.iso(end))
    body = build_query(start, end, service=service, level=level_key, text=text, trace_id=trace_id,
                       size=max(SAMPLE_SIZE, limit), timeout=timeout)
    path = f"/{index}/_search"
    status_code, resp = _http.get_json(
        base_url=base_url, path=path, allowed=_http.path_in({path}), timeout=timeout,
        params={"ignore_unavailable": "true", "allow_no_indices": "true"},
        json_body=body, max_bytes=MAX_RESPONSE_BYTES, backend="OpenSearch",
    )
    return _interpret(base, status_code, resp, limit)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validate(service, level, text, minutes, limit, trace_id) -> str | None:
    problem = (
        _http.validate_optional_text(service, "service", 100)
        or _http.validate_optional_text(text, "text", 200)
        or _http.validate_minutes(minutes, 1, MAX_MINUTES)
        or _http.validate_limit(limit, MAX_LIMIT)
    )
    if problem:
        return problem
    if level is not None and (not isinstance(level, str) or _normalise_level(level) is None):
        return f"level must be one of {', '.join(LEVELS)}."
    if trace_id is not None and (not isinstance(trace_id, str) or not _TRACE_ID_RE.match(trace_id)):
        return "trace_id must be 32 hexadecimal characters."
    return None


def _normalise_level(level: str) -> str | None:
    key = level.strip().lower()
    key = _LEVEL_ALIASES.get(key, key)
    return key if key in LEVELS else None


# --------------------------------------------------------------------------- #
# Query construction (pure; unit-tested)
# --------------------------------------------------------------------------- #
def _severity_clause(lo: int, hi: int, texts: tuple[str, ...]) -> dict[str, Any]:
    """Records whose severity.number is in [lo, hi], or which only have a matching severity.text."""
    variants = sorted({v for t in texts for v in (t, t.upper(), t.capitalize())})
    return {"bool": {"should": [
        {"range": {"severity.number": {"gte": lo, "lte": hi}}},
        {"bool": {"must_not": [{"exists": {"field": "severity.number"}}],
                  "filter": [{"terms": {"severity.text.keyword": variants}}]}},
    ], "minimum_should_match": 1}}


def _min_level_clause(level: str) -> dict[str, Any]:
    names = list(LEVELS)
    selected = names[names.index(level):]
    lo = LEVELS[selected[0]][0]
    texts = tuple(t for name in selected for t in LEVELS[name][2])
    return _severity_clause(lo, 24, texts)


def build_query(start: float, end: float, *, service=None, level=None, text=None, trace_id=None,
                size: int = SAMPLE_SIZE, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    flt: list[dict] = [{"range": {"observedTimestamp": {
        "gte": _iso_ms(start), "lte": _iso_ms(end), "format": "strict_date_optional_time"}}}]
    if service:
        flt.append({"term": {"resource.service.name.keyword": service}})
    if level:
        flt.append(_min_level_clause(level))
    if trace_id:
        flt.append({"term": {"traceId.keyword": trace_id.lower()}})
    must = [{"match": {"body": {"query": text, "operator": "and"}}}] if text else []
    must_not = [
        {"terms": {"resource.service.name.keyword": sorted(filters.RESTRICTED_SERVICES)}},
        {"query_string": {"query": _SERVER_SIDE_EXCLUSION, "fields": ["body", "eventName"],
                          "analyze_wildcard": True}},
        {"query_string": {"query": "_exists_:attributes.feature_flag*"}},
    ]
    return {
        "size": size,
        "timeout": f"{timeout:g}s",
        "track_total_hits": True,
        "sort": [{"observedTimestamp": {"order": "desc"}}],
        # Only the service name from `resource` (the rest is ~40 host/SDK attributes per record).
        "_source": ["@timestamp", "observedTimestamp", "severity", "body", "eventName", "traceId", "spanId",
                    "attributes", "resource.service.name"],
        "query": {"bool": {"filter": flt, "must": must, "must_not": must_not}},
        "aggs": {
            "by_service": {"terms": {"field": "resource.service.name.keyword", "size": 40}},
            "by_level": {"filters": {"filters": {
                name: _severity_clause(lo, hi, texts) for name, (lo, hi, texts) in LEVELS.items()}}},
            "first_seen": {"min": {"field": "observedTimestamp"}},
            "last_seen": {"max": {"field": "observedTimestamp"}},
        },
    }


def _iso_ms(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------- #
# Response interpretation
# --------------------------------------------------------------------------- #
def _interpret(base: dict, status_code: int, resp: Any, limit: int) -> dict[str, Any]:
    if not isinstance(resp, dict):
        return _http.error(base, "http_error",
                           f"HTTP {status_code} with a non-JSON body: {filters.redact_text(_http.snippet(resp))!r}")
    if status_code >= 400 or "error" in resp:
        return _http.error(base, *_opensearch_error(status_code, resp))
    hits = resp.get("hits")
    if not isinstance(hits, dict):
        return _http.error(base, "invalid_response", "OpenSearch returned an unexpected payload.")

    warnings = []
    if resp.get("timed_out"):
        warnings.append("OpenSearch timed out; results are partial.")
    if (resp.get("_shards") or {}).get("failed"):
        warnings.append("Some shards failed; results may be incomplete.")

    records = []
    for hit in hits.get("hits") or []:
        record = filters.sanitize_log(_normalise(hit.get("_source") or {}))
        if record is not None:
            records.append(record)

    total = (hits.get("total") or {}).get("value", 0)
    aggs = resp.get("aggregations") or {}
    out = dict(base)
    if warnings:
        out["warnings"] = warnings
    if total == 0 or not records:
        out.update(status="no_data", total_matches=0, records=[], patterns=[],
                   message="No log records matched in this window.")
        return out

    by_service = {b["key"]: b["doc_count"] for b in (aggs.get("by_service") or {}).get("buckets", [])
                  if not filters.is_restricted_service(b.get("key"))}
    by_level = {k: v["doc_count"] for k, v in ((aggs.get("by_level") or {}).get("buckets") or {}).items()
                if v.get("doc_count")}
    unspecified = total - sum(by_level.values())
    if unspecified > 0:
        by_level["unspecified"] = unspecified

    out.update(
        status="ok",
        total_matches=total,
        first_seen=(aggs.get("first_seen") or {}).get("value_as_string"),
        last_seen=(aggs.get("last_seen") or {}).get("value_as_string"),
        by_service=by_service,
        by_level=by_level,
        sample_size=len(records),
        patterns=_patterns(records),
        records=[_compact(r) for r in records[:limit]],
        truncated=total > min(limit, len(records)),
    )
    return out


def _opensearch_error(status_code: int, resp: dict) -> tuple[str, str]:
    err = resp.get("error")
    if isinstance(err, dict):
        reason = err.get("reason") or err.get("type") or "unknown error"
        root = (err.get("root_cause") or [{}])[0]
        if root.get("reason") and root.get("reason") != reason:
            reason = f"{reason}: {root['reason']}"
        kind = err.get("type", "")
    else:
        reason, kind = str(err), ""
    message = f"HTTP {status_code}: {filters.redact_text(str(reason))[:300]}"
    if status_code == 400 or kind in ("parsing_exception", "search_phase_execution_exception", "query_shard_exception"):
        return "bad_query", message
    if status_code in (429, 502, 503, 504):
        return "unavailable", message
    if status_code >= 500:
        return "backend_internal", message
    return "http_error", message


def _get(d: dict, dotted: str) -> Any:
    """Read ``a.b.c`` from a mapping that may use nested or dotted keys."""
    if dotted in d:
        return d[dotted]
    head, _, rest = dotted.partition(".")
    if rest and isinstance(d.get(head), dict):
        return _get(d[head], rest)
    return None


def _normalise(src: dict) -> dict[str, Any]:
    severity = src.get("severity") or {}
    number = severity.get("number")
    level = _level_name(number, severity.get("text"))
    ts = src.get("@timestamp")
    if not ts or str(ts).startswith("1970-"):
        ts = src.get("observedTimestamp")
    return {
        "time": ts,
        "service": _get(src.get("resource") or {}, "service.name") or "unknown",
        "level": level,
        "body": src.get("body") if isinstance(src.get("body"), str) else str(src.get("body") or ""),
        "event_name": src.get("eventName"),
        "trace_id": src.get("traceId") or None,
        "span_id": src.get("spanId") or None,
        "attributes": src.get("attributes") or {},
    }


def _level_name(number: Any, text: Any) -> str:
    if isinstance(number, int):
        for name, (lo, hi, _) in LEVELS.items():
            if lo <= number <= hi:
                return name
    if isinstance(text, str) and text.strip():
        t = text.strip().lower()
        for name, (_, _, texts) in LEVELS.items():
            if t in texts:
                return name
    return "unspecified"


def _template(message: str) -> str:
    for pattern, repl in _TEMPLATE_RULES:
        message = pattern.sub(repl, message)
    return " ".join(message.split())[:200]


def _patterns(records: list[dict]) -> list[dict[str, Any]]:
    groups: dict[tuple, dict[str, Any]] = {}
    for r in records:  # newest first
        message = r["body"] or (r["event_name"] or "")
        key = (r["service"], r["level"], _template(message))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"service": r["service"], "level": r["level"], "pattern": key[2],
                               "example": _truncate(message, MAX_MESSAGE_CHARS), "count": 0,
                               "first_seen": r["time"], "last_seen": r["time"], "trace_ids": []}
        g["count"] += 1
        g["first_seen"] = r["time"]  # records arrive newest first
        if r["trace_id"] and len(g["trace_ids"]) < 3 and r["trace_id"] not in g["trace_ids"]:
            g["trace_ids"].append(r["trace_id"])
    severity_rank = {name: i for i, name in enumerate(["unspecified", *LEVELS])}
    ordered = sorted(groups.values(), key=lambda g: (-severity_rank.get(g["level"], 0), -g["count"]))
    return ordered[:MAX_PATTERNS]


def _compact(r: dict) -> dict[str, Any]:
    attrs = {}
    for k, v in _flatten(r["attributes"]):
        if k.startswith(_NOISY_ATTRIBUTE_PREFIXES) or k.startswith("@"):
            continue
        attrs[k] = _truncate(v, MAX_ATTRIBUTE_CHARS) if isinstance(v, str) else v
        if len(attrs) >= MAX_ATTRIBUTES:
            break
    out = {"time": r["time"], "service": r["service"], "level": r["level"],
           "message": _truncate(r["body"] or (r["event_name"] or ""), MAX_MESSAGE_CHARS)}
    if r["event_name"]:
        out["event_name"] = r["event_name"]
    if r["trace_id"]:
        out["trace_id"] = r["trace_id"]
    if attrs:
        out["attributes"] = attrs
    return out


def _flatten(d: dict, prefix: str = ""):
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _flatten(v, key + ".")
        elif isinstance(v, (str, int, float, bool)) or v is None:
            yield key, v


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
