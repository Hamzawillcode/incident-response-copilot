"""Live integration tests against the running OpenTelemetry Demo Prometheus.

Skipped automatically when Prometheus is not reachable, so the unit suite stays
hermetic. Run only these with:  pytest -m live
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from copilot.tools.metrics import DEFAULT_PROMETHEUS_URL, query_metrics

PROM = os.environ.get("PROMETHEUS_URL", DEFAULT_PROMETHEUS_URL)


def _prometheus_ready() -> bool:
    try:
        return httpx.get(f"{PROM.rstrip('/')}/-/ready", timeout=2, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _prometheus_ready(), reason=f"Prometheus not reachable at {PROM}"),
]


def test_live_request_rate_per_service():
    r = query_metrics("sum by (service_name) (rate(traces_span_metrics_calls_total[5m]))", 15)
    assert r["status"] == "ok", r
    names = {s["labels"]["service_name"] for s in r["series"]}
    assert {"frontend", "checkout", "cart"} <= names
    assert not any("flag" in n for n in names)


def test_live_container_memory_gauge():
    r = query_metrics('container_memory_usage_total_bytes{container_name="checkout"}', 10)
    assert r["status"] == "ok", r
    assert r["series"][0]["latest"] > 0


def test_live_bad_query_is_reported():
    r = query_metrics("sum(rate(traces_span_metrics_calls_total[5m])", 5)
    assert r["status"] == "error" and r["error"]["type"] == "bad_query"


def test_live_result_contains_no_flag_information():
    r = query_metrics("sum by (job) (rate(traces_span_metrics_calls_total[5m]))", 15, max_series=50)
    assert r["status"] == "ok"
    assert "flag" not in json.dumps(r).lower()
