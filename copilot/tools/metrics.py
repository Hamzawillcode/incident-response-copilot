"""Read-only Prometheus metrics tool for the Incident Response Copilot.

Public API:
    query_metrics(promql: str, minutes: int, max_series: int = 20) -> dict

Runs a PromQL *range* query over the last ``minutes`` minutes via
``GET {PROMETHEUS_URL}/api/v1/query_range`` and returns a compact summary per
series (min, max, latest, trend, ...) instead of raw samples. It never raises:
every outcome comes back as a dict with ``status`` = ``"ok"`` | ``"no_data"`` |
``"error"``.

Safety properties
-----------------
* Read-only: the only HTTP call is a GET to ``/api/v1/query_range`` (via
  ``copilot.tools._http.get_json`` with an allow-list). The demo's Prometheus
  runs with ``--web.enable-lifecycle``, so POST ``/-/quit`` would stop it.
* No proxy: ``HTTP(S)_PROXY`` / ``ALL_PROXY`` are ignored.
* Feature-flag isolation (``copilot.tools.filters``): flag-related queries,
  ``__name__`` and metric-less ``{...}`` selectors are refused before any
  request; flag-labelled series are dropped; Prometheus text is redacted; the
  whole result passes through ``filters.scrub_output``.

Configuration (environment variables)
-------------------------------------
PROMETHEUS_URL               default ``http://localhost:9090``
PROMETHEUS_TIMEOUT_SECONDS   default ``10`` (0 < t <= 120)
PROMETHEUS_MIN_STEP_SECONDS  default ``60`` (the demo's measured sample interval)
"""

from __future__ import annotations

import math
import re
import statistics
from typing import Any

from copilot.tools import _http, filters

__all__ = ["query_metrics"]

DEFAULT_PROMETHEUS_URL = "http://localhost:9090"
DEFAULT_TIMEOUT_SECONDS = 10.0

QUERY_RANGE_PATH = "/api/v1/query_range"
_ALLOWED = _http.path_in({QUERY_RANGE_PATH})

MIN_MINUTES = 1
MAX_MINUTES = 7 * 24 * 60  # Prometheus retention in the demo is 7 days
MAX_QUERY_LENGTH = 2_000
DEFAULT_MIN_STEP_SECONDS = 60  # measured: the demo's OTLP metrics arrive every 60 s
MAX_POINTS_PER_SERIES = 240
DEFAULT_MAX_SERIES = 20
MAX_SERIES_LIMIT = 200
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
TREND_THRESHOLD = 0.10  # |recent - early| / |mean| needed to call a direction
MAX_MESSAGE_CHARS = 300

_STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`')
# A '{' that does not directly follow a metric name, i.e. a selector like {job="x"}.
_BARE_SELECTOR = re.compile(r"(?:^|[^\w:\s])\s*\{")

# Resource labels that add bulk but no diagnostic value to a summary.
_NOISY_LABEL_PREFIXES = (
    "host_",
    "os_",
    "process_runtime_",
    "telemetry_sdk_",
    "collector_instance_id",
    "service_instance_id",
    "service_namespace",
    "service_version",
    "service_criticality",
)

_PROMETHEUS_ERROR_TYPES = {
    "bad_data": "bad_query",
    "execution": "query_execution",
    "timeout": "timeout",
    "canceled": "timeout",
    "unavailable": "unavailable",
    "internal": "prometheus_internal",
    "not_found": "not_found",
}

_now = _http.now  # module-level so tests can pin the clock


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def query_metrics(promql: str, minutes: int, max_series: int = DEFAULT_MAX_SERIES) -> dict[str, Any]:
    """Summarise a PromQL expression over the last ``minutes`` minutes.

    Use this to see how a metric behaved recently: request rates, error ratios,
    latency percentiles, memory or CPU. Each matching series is summarised
    (min, max, mean, first, latest, change, trend) instead of returning raw
    samples. Prefer aggregated queries, e.g.
    ``sum by (service_name) (rate(traces_span_metrics_calls_total[5m]))``,
    and rate windows of at least ``[2m]`` (samples arrive every 60 s).

    Args:
        promql: PromQL expression. Selectors must start with a metric name.
        minutes: Look-back window in minutes, 1 to 10080 (7 days).
        max_series: Maximum number of series summaries to return, 1 to 200
            (largest latest value first). Extra series are counted, not shown.

    Returns:
        dict with ``status`` ``"ok"`` (with ``series``), ``"no_data"`` or
        ``"error"`` (with ``error.type`` and ``error.message``).
    """
    base: dict[str, Any] = {"query": promql, "window_minutes": minutes}
    try:
        result = _query(base, promql, minutes, max_series)
    except _http.ToolError as exc:
        result = _http.error(base, exc.type, exc.message)
    except Exception as exc:  # last resort: a malformed payload must not crash the caller
        result = _http.error(base, "internal", f"Unexpected {type(exc).__name__} while processing the request.")
    return filters.scrub_output(result)


def _query(base: dict, promql: Any, minutes: Any, max_series: Any) -> dict[str, Any]:
    problem = _validate(promql, minutes, max_series)
    if problem:
        return _http.error(base, "invalid_input", problem)
    refusal = _guard_query(promql)
    if refusal:
        return _http.error(base, "forbidden_query", refusal)

    base_url = _http.base_url_from_env("PROMETHEUS_URL", DEFAULT_PROMETHEUS_URL)
    end = _now()
    start = end - minutes * 60
    step = _step_seconds(minutes)
    timeout = _timeout_seconds()
    params = {
        "query": promql,
        "start": f"{start:.3f}",
        "end": f"{end:.3f}",
        "step": str(step),
        "timeout": f"{timeout:g}s",  # ask Prometheus to give up before we do
    }
    base.update({"start": _http.iso(start), "end": _http.iso(end), "step_seconds": step})
    status_code, body = _http.get_json(
        base_url=base_url, path=QUERY_RANGE_PATH, allowed=_ALLOWED, timeout=timeout,
        params=params, max_bytes=MAX_RESPONSE_BYTES, backend="Prometheus",
    )
    return _interpret(base, status_code, body, end, max_series)


# --------------------------------------------------------------------------- #
# Validation and the query guard
# --------------------------------------------------------------------------- #
def _validate(promql: Any, minutes: Any, max_series: Any) -> str | None:
    if not isinstance(promql, str) or not promql.strip():
        return "promql must be a non-empty string."
    if len(promql) > MAX_QUERY_LENGTH:
        return f"promql is longer than {MAX_QUERY_LENGTH} characters."
    return _http.validate_minutes(minutes, MIN_MINUTES, MAX_MINUTES) or _http.validate_limit(
        max_series, MAX_SERIES_LIMIT, "max_series"
    )


def _guard_query(promql: str) -> str | None:
    refusal = filters.refuse_inputs(promql)  # full text, string literals included
    if refusal:
        return "This query targets restricted telemetry and is not permitted."
    code = _STRING_LITERAL.sub('""', promql)
    if "__name__" in code:
        return "Selecting metrics by __name__ is not permitted; name the metric explicitly."
    if _BARE_SELECTOR.search(code):
        return "Selectors must start with a metric name, e.g. http_server_request_duration_seconds_count{...}."
    return None


def _redact(text: str) -> str:
    text = filters.redact_text(text)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"
    return text


# --------------------------------------------------------------------------- #
# Response interpretation and summarisation
# --------------------------------------------------------------------------- #
def _interpret(base: dict, status_code: int, body: Any, end: float, max_series: int) -> dict[str, Any]:
    if not isinstance(body, dict):
        return _http.error(base, "http_error",
                           f"HTTP {status_code} with a non-JSON body: {_redact(_http.snippet(body))!r}")

    if body.get("status") == "error" or status_code >= 400:
        err_type = _PROMETHEUS_ERROR_TYPES.get(str(body.get("errorType")), "http_error")
        message = body.get("error") or f"HTTP {status_code}"
        return _http.error(base, err_type, f"HTTP {status_code}: {_redact(str(message))}")

    data = body.get("data")
    if body.get("status") != "success" or not isinstance(data, dict):
        return _http.error(base, "invalid_response", "Prometheus returned an unexpected payload.")

    notes = [_redact(str(n)) for n in (body.get("warnings") or []) + (body.get("infos") or [])]
    result_type = data.get("resultType")
    raw_series = data.get("result") or []
    if result_type != "matrix" or not isinstance(raw_series, list):
        return _http.error(base, "invalid_response", f"Expected a matrix result, got {result_type!r}.")

    # Feature-flag isolation: drop silently (a count would itself be a signal).
    visible = [s for s in raw_series if not filters.labels_are_restricted(s.get("metric") or {})]
    summaries = [_summarise(s, end) for s in visible]
    summaries.sort(key=lambda s: (s["latest"] is None, -(s["latest"] or 0.0)))

    out = dict(base)
    if notes:
        out["warnings"] = notes
    if not summaries:
        out.update(status="no_data", series_count=0, series=[],
                   message="The query is valid but matched no data in this window.")
        return out
    out.update(
        status="ok",
        series_count=len(summaries),
        returned_series=min(len(summaries), max_series),
        truncated=len(summaries) > max_series,
        series=summaries[:max_series],
    )
    return out


def _summarise(series: dict, end: float) -> dict[str, Any]:
    labels = {
        ("metric" if k == "__name__" else k): v
        for k, v in sorted((series.get("metric") or {}).items())
        if not k.startswith(_NOISY_LABEL_PREFIXES)
    }
    points: list[tuple[float, float]] = []
    non_finite = 0
    for pair in series.get("values") or []:
        try:
            ts, val = float(pair[0]), float(pair[1])  # Prometheus sends values as strings
        except (TypeError, ValueError, IndexError):
            non_finite += 1
            continue
        if math.isfinite(val):
            points.append((ts, val))
        else:
            non_finite += 1  # "NaN", "+Inf", "-Inf"

    summary: dict[str, Any] = {"labels": labels, "points": len(points), "non_finite_points": non_finite}
    if not points:
        summary.update(min=None, max=None, mean=None, first=None, latest=None, latest_timestamp=None,
                       latest_age_seconds=None, change=None, change_percent=None, trend="insufficient_data",
                       early_median=None, recent_median=None)
        return summary

    values = [v for _, v in points]
    first, (latest_ts, latest) = values[0], points[-1]
    change = latest - first
    trend, early, recent = _trend(values)
    r = _http.round6
    summary.update(
        min=r(min(values)),
        max=r(max(values)),
        mean=r(statistics.fmean(values)),
        first=r(first),
        latest=r(latest),
        latest_timestamp=_http.iso(latest_ts),
        latest_age_seconds=round(max(0.0, end - latest_ts), 1),
        change=r(change),
        change_percent=r(change / abs(first) * 100) if first != 0 else None,
        trend=trend,
        early_median=None if early is None else r(early),
        recent_median=None if recent is None else r(recent),
    )
    return summary


def _trend(values: list[float]) -> tuple[str, float | None, float | None]:
    """Classify the direction of a series over the window.

    Compares the median of the most recent quarter of points with the median of
    the earliest quarter, relative to the series mean. Medians make a single
    noisy sample unable to flip the verdict; quarters make the answer describe
    the whole window rather than two arbitrary endpoints.

    Returns (trend, early_median, recent_median).
    """
    if len(values) < 2:
        return "insufficient_data", None, None
    k = max(1, len(values) // 4)
    early = statistics.median(values[:k])
    recent = statistics.median(values[-k:])
    if max(values) == min(values):
        return "flat", early, recent
    relative = (recent - early) / max(abs(statistics.fmean(values)), 1e-12)
    if relative > TREND_THRESHOLD:
        return "increasing", early, recent
    if relative < -TREND_THRESHOLD:
        return "decreasing", early, recent
    return "flat", early, recent


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _step_seconds(minutes: int) -> int:
    return max(_min_step_seconds(), math.ceil(minutes * 60 / MAX_POINTS_PER_SERIES))


def _min_step_seconds() -> int:
    """Smallest evaluation step. Stepping faster than samples arrive only repeats values."""
    return _http.int_from_env("PROMETHEUS_MIN_STEP_SECONDS", DEFAULT_MIN_STEP_SECONDS, 1, 3600)


def _timeout_seconds() -> float:
    return _http.float_from_env("PROMETHEUS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 0, 120)
