"""Record real Prometheus /api/v1/query_range responses as pytest fixtures.

Usage (with the OpenTelemetry Demo running):
    .venv/bin/python scripts/record_prometheus_fixtures.py

Read-only: issues GET requests to /api/v1/query_range only.
Each fixture stores the exact request parameters and the raw HTTP response
(status code, content type, body) so tests can replay it without Prometheus.

Note: this recorder deliberately bypasses the copilot's feature-flag query guard
so that we can capture a flag-bearing response and prove the result scrubber
removes it. Fixtures are test data and are never shown to an agent.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "prometheus"
WINDOW_MINUTES = 30
STEP_SECONDS = 15

# name -> (description, promql)
FIXTURES: dict[str, tuple[str, str]] = {
    "request_rate_by_service": (
        "Span-metric request rate per service (multi-series matrix; includes flagd/flagd-ui series).",
        "sum by (service_name) (rate(traces_span_metrics_calls_total[5m]))",
    ),
    "checkout_p95_latency": (
        "p95 latency of checkout from span-metric histograms (single series).",
        'histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{service_name="checkout"}[5m])))',
    ),
    "frontend_memory_usage": (
        "Memory usage of the frontend container (gauge from the collector's docker_stats receiver).",
        'container_memory_usage_total_bytes{container_name="frontend"}',
    ),
    "all_container_memory": (
        "Raw selector over all 28 containers (many series; includes the flagd container).",
        "container_memory_usage_total_bytes",
    ),
    "empty_result": (
        "Valid query that matches no series.",
        'traces_span_metrics_calls_total{service_name="does-not-exist"}',
    ),
    "nan_values": (
        "Division by zero: Prometheus encodes the samples as the string \"NaN\".",
        "vector(0) / vector(0)",
    ),
    "bad_syntax": (
        "Unclosed parenthesis: HTTP 400 errorType=bad_data.",
        "sum(rate(traces_span_metrics_calls_total[5m])",
    ),
    "execution_error": (
        "Many-to-many vector match: HTTP 422 errorType=execution.",
        "traces_span_metrics_calls_total + on(job) traces_span_metrics_calls_total",
    ),
    "rate_on_gauge_warning": (
        "rate() applied to a gauge: success with an info annotation.",
        'rate(system_cpu_utilization_ratio{state="user"}[5m])',
    ),
    "flag_metric_leak": (
        "Feature-flag evaluation metric with flag key labels (must be scrubbed).",
        "sum by (service_name, feature_flag_key) (feature_flag_evaluation_requests_total)",
    ),
}


def record(client: httpx.Client, name: str, description: str, promql: str, end: float) -> None:
    params = {
        "query": promql,
        "start": f"{end - WINDOW_MINUTES * 60:.3f}",
        "end": f"{end:.3f}",
        "step": f"{STEP_SECONDS}",
    }
    resp = client.get("/api/v1/query_range", params=params)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    fixture = {
        "description": description,
        "recorded_at": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(),
        "prometheus_url": PROMETHEUS_URL,
        "request": {"method": "GET", "path": "/api/v1/query_range", "params": params},
        "response": {
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "body": body,
        },
    }
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(fixture, indent=1) + "\n")
    n = len(body.get("data", {}).get("result", [])) if isinstance(body, dict) else "-"
    print(f"{name:26s} HTTP {resp.status_code}  series={n}  -> {path.name}")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    end = time.time()
    with httpx.Client(base_url=PROMETHEUS_URL, timeout=20, trust_env=False) as client:
        for name, (description, promql) in FIXTURES.items():
            record(client, name, description, promql, end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
