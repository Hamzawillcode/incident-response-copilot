"""Live integration tests for search_logs and get_traces against the running demo.

Require deploy/compose.copilot.yaml to be applied (OpenSearch on 127.0.0.1:9200,
Jaeger on 127.0.0.1:16686). Auto-skipped when a backend is unreachable.
Run only these with:  pytest -m live
"""

from __future__ import annotations

import json
import os
import re

import httpx
import pytest

from copilot.tools import filters, get_traces, query_metrics, search_logs
from copilot.tools.logs import DEFAULT_OPENSEARCH_URL
from copilot.tools.traces import DEFAULT_JAEGER_URL

OPENSEARCH = os.environ.get("OPENSEARCH_URL", DEFAULT_OPENSEARCH_URL).rstrip("/")
JAEGER = os.environ.get("JAEGER_URL", DEFAULT_JAEGER_URL).rstrip("/")
LEAK = re.compile(r"flag|ofrep|openfeature|" + "|".join(filters.FLAG_KEYS), re.IGNORECASE)
SHOP_SERVICES = ["frontend-proxy", "frontend", "checkout", "cart", "payment", "product-catalog", "currency",
                 "shipping", "quote", "email", "recommendation", "ad", "accounting", "fraud-detection",
                 "load-generator"]


def _up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


OPENSEARCH_UP = _up(f"{OPENSEARCH}/_cluster/health")
JAEGER_UP = _up(f"{JAEGER}/api/services")
needs_opensearch = pytest.mark.skipif(not OPENSEARCH_UP, reason=f"OpenSearch not reachable at {OPENSEARCH}")
needs_jaeger = pytest.mark.skipif(not JAEGER_UP, reason=f"Jaeger not reachable at {JAEGER}")
pytestmark = pytest.mark.live


@needs_opensearch
def test_live_recent_logs():
    r = search_logs(minutes=10, limit=10)
    assert r["status"] == "ok", r
    assert r["total_matches"] > 0 and "frontend-proxy" in r["by_service"]
    assert not any(filters.is_restricted_service(s) for s in r["by_service"])
    assert len(r["records"]) == 10


@needs_opensearch
def test_live_level_and_service_filters():
    r = search_logs(service="checkout", level="info", minutes=30, limit=5)
    assert r["status"] in ("ok", "no_data"), r
    if r["status"] == "ok":
        assert set(r["by_service"]) == {"checkout"}
        assert set(r["by_level"]) <= {"info", "warn", "error", "fatal"}


@needs_opensearch
def test_live_shipping_logs_are_found_despite_epoch_timestamps():
    """shipping (Rust) logs carry @timestamp=1970; filtering on observedTimestamp must still find them."""
    r = search_logs(service="shipping", minutes=30, limit=3)
    assert r["status"] == "ok", r
    assert all(not str(x["time"]).startswith("1970") for x in r["records"])


@needs_jaeger
def test_live_checkout_traces():
    r = get_traces(service="checkout", minutes=10, limit=3)
    assert r["status"] == "ok", r
    assert all("flagd" not in t["services"] for t in r["traces"])
    assert r["summary"]["duration_ms"]["p50"] > 0


@needs_jaeger
@needs_opensearch
def test_live_trace_to_logs_correlation():
    traces = get_traces(service="checkout", operation="oteldemo.CheckoutService/PlaceOrder", minutes=15, limit=1)
    assert traces["status"] == "ok", traces
    trace_id = traces["traces"][0]["trace_id"]
    single = get_traces(trace_id=trace_id)
    assert single["status"] == "ok" and single["traces"][0]["trace_id"] == trace_id
    logs = search_logs(trace_id=trace_id, minutes=30, limit=50)
    assert logs["status"] in ("ok", "no_data"), logs
    if logs["status"] == "ok":
        assert all(x.get("trace_id") == trace_id for x in logs["records"])


@needs_jaeger
@needs_opensearch
def test_live_no_flag_information_from_any_tool():
    outputs = [query_metrics("sum by (job) (rate(traces_span_metrics_calls_total[5m]))", 15, max_series=200),
               search_logs(minutes=15, limit=100), search_logs(level="warn", minutes=60, limit=100)]
    for svc in SHOP_SERVICES:
        outputs.append(get_traces(service=svc, minutes=15, limit=5))
        outputs.append(get_traces(service=svc, errors_only=True, minutes=60, limit=5))
        outputs.append(search_logs(service=svc, minutes=15, limit=20))
    text = json.dumps(outputs)
    assert not LEAK.search(text), LEAK.search(text).group(0)
    assert all(o["status"] in ("ok", "no_data") for o in outputs), [o.get("error") for o in outputs if o["status"] == "error"]
