"""Service health overview tools: known-good PromQL behind simple parameters.

Public API:
    get_service_health(minutes: int = 15) -> dict
    get_service_metrics(service: str, minutes: int = 15) -> dict

Why these exist
---------------
An LLM writing raw PromQL often guesses a wrong metric or label name, gets
``no_data`` and wrongly concludes that nothing is broken. These two tools run a
fixed set of tested queries (the "golden signals" from the span metrics:
traffic, error ratio, p95 latency) and return one compact row per service or
per operation. The agent starts here and only writes its own PromQL with
``query_metrics`` for unusual questions (memory, queue lag, ...).

They are thin wrappers: every query goes through ``query_metrics``, so they
inherit its read-only HTTP path, flag-isolation filters and never-raise
contract. Only server and consumer spans are counted, so each service is
measured on the work it *receives* (a client span failing in checkout because
payment failed is counted in payment, not twice).

The ``observations`` strings are plain comparisons of the end of the window
with its start. They describe symptoms; they never name a cause.
"""

from __future__ import annotations

import re
from typing import Any

from copilot.tools import _http, filters
from copilot.tools.metrics import query_metrics

__all__ = ["get_service_health", "get_service_metrics"]

MIN_MINUTES = 5          # rate()[2m] needs a few 60 s samples to say anything
MAX_MINUTES = 6 * 60
RATE_WINDOW = "2m"
MAX_OPERATIONS = 10

# Observation thresholds (documented in DECISIONS.md D-302).
ERROR_PCT_NOTABLE = 5.0      # error ratio worth mentioning, in %
LATENCY_FACTOR = 2.0         # p95 now vs window start
LATENCY_MIN_MS = 50.0        # ignore doubling of tiny latencies (2 ms -> 4 ms)
LATENCY_MIN_RPS = 0.05       # p95 of a handful of requests per minute is noise
TRAFFIC_FACTOR = 2.0
TRAFFIC_MIN_RPS = 0.5

SPAN_KINDS = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
_ERROR = 'status_code="STATUS_CODE_ERROR"'
_SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,62}")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def get_service_health(minutes: int = 15) -> dict[str, Any]:
    """Overview of every service: traffic, error ratio and p95 latency. START HERE.

    Runs tested PromQL queries for you (no PromQL needed) and returns one row
    per service with the value now, at the start of the window and the
    maximum, plus plain-language ``observations`` for anything that changed
    (for example "error ratio 48% now vs 0% at window start"). Services with
    observations are listed first. Use it first in every investigation to see
    which services look unhealthy, then drill down with ``get_service_metrics``,
    ``get_traces`` and ``search_logs``.

    Args:
        minutes: Look-back window in minutes, 5 to 360. 15 to 30 is usually right.

    Returns:
        dict with ``status`` ``"ok"`` (with ``services``), ``"no_data"`` or
        ``"error"``. Each service row has ``requests_per_sec``, ``error_pct``
        and ``p95_ms`` (each with ``now``, ``window_start`` and ``max``) and
        ``observations``.
    """
    base: dict[str, Any] = {"window_minutes": minutes}
    try:
        problem = _http.validate_minutes(minutes, MIN_MINUTES, MAX_MINUTES)
        if problem:
            return filters.scrub_output(_http.error(base, "invalid_input", problem))
        rows, problems = _collect(SPAN_KINDS, "service_name", minutes)
        result = _assemble(base, rows, problems, key="service", limit=None)
    except Exception as exc:  # last resort, same contract as the other tools
        result = _http.error(base, "internal", f"Unexpected {type(exc).__name__} while building the overview.")
    return filters.scrub_output(result)


def get_service_metrics(service: str, minutes: int = 15) -> dict[str, Any]:
    """Detail for ONE service: its overall golden signals and its operations.

    Use after ``get_service_health`` points at a service. Returns the
    service-level row (traffic, error ratio, p95 latency) and up to 10
    operations (span names, e.g. ``charge`` or ``oteldemo.CartService/GetCart``)
    sorted with the most erroring / slowest first, so you can see *which*
    endpoint of the service is failing or slow.

    Args:
        service: Service name exactly as listed by ``get_service_health``,
            e.g. "payment" or "checkout".
        minutes: Look-back window in minutes, 5 to 360.

    Returns:
        dict with ``status`` ``"ok"`` (with ``service_summary`` and
        ``operations``), ``"no_data"`` (unknown service or no traffic) or
        ``"error"``.
    """
    base: dict[str, Any] = {"service": service, "window_minutes": minutes}
    try:
        problem = _validate_service(service) or _http.validate_minutes(minutes, MIN_MINUTES, MAX_MINUTES)
        if problem:
            return filters.scrub_output(_http.error(base, "invalid_input", problem))
        if filters.refuse_inputs(service):
            return filters.scrub_output(_http.error(base, "forbidden_query", filters.REFUSAL_MESSAGE))
        selector = f'{SPAN_KINDS},service_name="{service}"'
        svc_rows, svc_problems = _collect(selector, "service_name", minutes)
        op_rows, op_problems = _collect(selector, "span_name", minutes)
        result = _assemble(base, op_rows, svc_problems + op_problems, key="operation", limit=MAX_OPERATIONS)
        if result["status"] == "ok":
            summary = next((r for r in svc_rows.values()), None)
            result = {**{k: v for k, v in result.items() if k != "rows"},
                      "service_summary": _strip_key(summary),
                      "operations": result["rows"]}
        else:
            result.pop("rows", None)
    except Exception as exc:
        result = _http.error(base, "internal", f"Unexpected {type(exc).__name__} while building the service detail.")
    return filters.scrub_output(result)


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def golden_signal_queries(selector: str, by: str, rate_window: str = RATE_WINDOW) -> dict[str, str]:
    """The three tested PromQL queries, grouped by ``by`` (service_name or span_name).

    ``rate_window`` trades speed for stability: 2m reacts fast (the agent's view);
    the detector uses 5m because a service with ~1 request a minute swings
    between 0 % and 100 % errors over 2 minutes.
    """
    calls = "traces_span_metrics_calls_total"
    total = f"sum by ({by}) (rate({calls}{{{selector}}}[{rate_window}]))"
    errors = f"sum by ({by}) (rate({calls}{{{selector},{_ERROR}}}[{rate_window}]))"
    return {
        "requests_per_sec": total,
        # `or 0 * total` gives services with traffic but no errors an explicit 0,
        # so the window start shows the healthy baseline instead of a gap.
        "error_pct": f"100 * ({errors} or 0 * {total}) / {total}",
        "p95_ms": (
            f"histogram_quantile(0.95, sum by (le, {by}) "
            f"(rate(traces_span_metrics_duration_milliseconds_bucket{{{selector}}}[{rate_window}])))"
        ),
    }


def _collect(selector: str, by: str, minutes: int) -> tuple[dict[str, dict], list[str]]:
    """Run the three queries and merge them into {group_value: row}."""
    rows: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for signal, promql in golden_signal_queries(selector, by).items():
        res = query_metrics(promql, minutes, max_series=200)
        if res.get("status") == "error":
            err = res.get("error") or {}
            problems.append(f"{signal}: {err.get('type')}: {err.get('message')}")
            continue
        for series in res.get("series") or []:
            name = (series.get("labels") or {}).get(by)
            if not name:
                continue
            rows.setdefault(name, {})[signal] = _stat(series, signal)
    return rows, problems


def _stat(series: dict, signal: str) -> dict[str, Any]:
    digits = {"requests_per_sec": 3, "error_pct": 1, "p95_ms": 1}[signal]

    def r(v):
        return None if v is None else round(float(v), digits)

    return {
        "now": r(series.get("recent_median") if series.get("recent_median") is not None else series.get("latest")),
        "window_start": r(series.get("early_median") if series.get("early_median") is not None else series.get("first")),
        "max": r(series.get("max")),
    }


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def _assemble(base: dict, rows: dict[str, dict], problems: list[str], *, key: str,
              limit: int | None) -> dict[str, Any]:
    out = dict(base)
    if not rows:
        if problems and len(problems) >= 3:
            return _http.error(base, "query_failed", "; ".join(problems)[:600])
        out.update(status="no_data", rows=[],
                   message="No span metrics in this window (unknown name, or no traffic).")
        return out

    table = []
    for name, signals in rows.items():
        row = {key: name}
        for signal in ("requests_per_sec", "error_pct", "p95_ms"):
            row[signal] = signals.get(signal)
        row["observations"] = observations(row)
        table.append(row)
    table.sort(key=_severity, reverse=True)

    out["status"] = "ok"
    if problems:
        out["warnings"] = problems
    if limit is not None and len(table) > limit:
        out["omitted"] = len(table) - limit
        table = table[:limit]
    if key == "service":
        out["services_with_observations"] = [r["service"] for r in table if r["observations"]]
        out["services"] = table
        out["note"] = ("error_pct = % of server/consumer spans with error status; values are medians of the "
                       "last and first quarter of the window. Observations compare the two, they are not causes.")
    else:
        out["rows"] = table
    return out


def observations(row: dict[str, Any]) -> list[str]:
    """Plain-language changes between the start and the end of the window."""
    notes: list[str] = []
    err, p95, rps = row.get("error_pct"), row.get("p95_ms"), row.get("requests_per_sec")

    if err and err.get("now") is not None and err["now"] >= ERROR_PCT_NOTABLE:
        start = err.get("window_start")
        notes.append(f"error ratio {err['now']:g}% now vs {start:g}% at window start"
                     if start is not None else f"error ratio {err['now']:g}% now")
    elif err and err.get("max") is not None and err["max"] >= ERROR_PCT_NOTABLE * 2:
        notes.append(f"error ratio peaked at {err['max']:g}% in the window (now {err.get('now') or 0:g}%)")

    busy = bool(rps) and (rps.get("now") or 0.0) >= LATENCY_MIN_RPS
    if busy and p95 and p95.get("now") is not None and p95.get("window_start"):
        now, start = p95["now"], p95["window_start"]
        if now >= LATENCY_MIN_MS and now >= LATENCY_FACTOR * start:
            notes.append(f"p95 latency {now:g} ms now vs {start:g} ms at window start ({now / start:.1f}x)")

    if rps and rps.get("now") is not None and rps.get("window_start"):
        now, start = rps["now"], rps["window_start"]
        if now >= TRAFFIC_MIN_RPS and now >= TRAFFIC_FACTOR * start:
            notes.append(f"traffic {now:g} req/s now vs {start:g} at window start ({now / start:.1f}x)")
        elif start >= TRAFFIC_MIN_RPS and now <= start / TRAFFIC_FACTOR:
            notes.append(f"traffic dropped to {now:g} req/s from {start:g} at window start")
    return notes


def _severity(row: dict[str, Any]) -> tuple:
    err = (row.get("error_pct") or {}).get("now") or 0.0
    p95 = row.get("p95_ms") or {}
    ratio = (p95.get("now") or 0.0) / p95["window_start"] if p95.get("window_start") else 0.0
    return (bool(row["observations"]), err, ratio, (row.get("requests_per_sec") or {}).get("now") or 0.0)


def _strip_key(row: dict | None) -> dict | None:
    if row is None:
        return None
    full = {"requests_per_sec": row.get("requests_per_sec"), "error_pct": row.get("error_pct"),
            "p95_ms": row.get("p95_ms")}
    full["observations"] = observations(full)
    return full


def _validate_service(service: Any) -> str | None:
    if not isinstance(service, str) or not _SERVICE_NAME.fullmatch(service):
        return "service must be a service name such as 'payment' (letters, digits, '-', '_', '.')."
    return None
