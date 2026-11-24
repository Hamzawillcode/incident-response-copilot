"""Tests for copilot.tools.traces.get_traces.

Replays responses recorded from the demo's Jaeger 2.19 query API
(scripts/record_tool_fixtures.py), including traces recorded while
productCatalogFailure and paymentFailure (50 %) were active.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from copilot.tools import filters, traces
from copilot.tools.traces import get_traces
from tests.conftest import load_fixture

LEAK = re.compile(r"flag|ofrep|openfeature|" + "|".join(filters.FLAG_KEYS), re.IGNORECASE)


def _no_leak(result) -> bool:
    return not LEAK.search(json.dumps(result))


# --------------------------------------------------------------------------- #
# Request shape (read-only)
# --------------------------------------------------------------------------- #
def test_search_request_parameters(replay, monkeypatch):
    monkeypatch.setattr(traces, "_now", lambda: 1_800_000_000.0)
    rec = replay("jaeger/traces_checkout")
    get_traces(service="checkout", operation="oteldemo.CheckoutService/PlaceOrder", errors_only=True,
               minutes=15, limit=5, min_duration_ms=250)
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "GET" and req.url.path == "/jaeger/ui/api/traces"
    p = dict(req.url.params)
    assert p == {
        "service": "checkout",
        "operation": "oteldemo.CheckoutService/PlaceOrder",
        "start": str((1_800_000_000 - 900) * 1_000_000),
        "end": str(1_800_000_000 * 1_000_000),
        "limit": "10",  # over-fetch 2x: sanitization can remove traces
        "tags": '{"error": "true"}',
        "minDuration": "250ms",
    }
    assert rec.base_urls == ["http://localhost:16686/jaeger/ui"]


def test_trace_id_request(replay):
    rec = replay("jaeger/trace_by_id")
    get_traces(trace_id="ABCDEF0123456789" * 2)
    assert rec.requests[0].method == "GET"
    assert rec.requests[0].url.path == "/jaeger/ui/api/traces/abcdef0123456789abcdef0123456789"
    assert dict(rec.requests[0].url.params) == {}


def test_jaeger_url_env_var(replay, monkeypatch):
    monkeypatch.setenv("JAEGER_URL", "http://jaeger.internal:16686/jaeger/ui/")
    rec = replay("jaeger/traces_unknown_service")
    get_traces(service="checkout")
    assert rec.base_urls == ["http://jaeger.internal:16686/jaeger/ui"]
    assert str(rec.requests[0].url).startswith("http://jaeger.internal:16686/jaeger/ui/api/traces?")


def test_allow_list_rejects_other_jaeger_paths(replay):
    from copilot.tools import _http
    allowed = lambda p: p == traces.SEARCH_PATH or bool(traces._TRACE_PATH_RE.match(p))  # noqa: E731
    for path in ("/api/services", "/api/traces/../admin", "/api/traces/xyz", "/api/v3/traces"):
        with pytest.raises(_http.ToolError):
            _http.get_json(base_url="http://j", path=path, allowed=allowed, timeout=1)


# --------------------------------------------------------------------------- #
# Summaries from recorded responses
# --------------------------------------------------------------------------- #
def test_checkout_search_summary(replay):
    replay("jaeger/traces_checkout")
    r = get_traces(service="checkout", minutes=15, limit=3)
    assert r["status"] == "ok" and r["returned_traces"] == 3
    s = r["summary"]
    d = s["duration_ms"]
    assert d["min"] <= d["p50"] <= d["p95"] <= d["max"]
    for t in r["traces"]:
        assert re.fullmatch(r"[0-9a-f]{32}", t["trace_id"])
        assert "checkout" in t["services"]
        assert "flagd" not in t["services"]
        assert t["duration_ms"] > 0 and t["span_count"] > 0
        assert len(t["relevant_spans"]) <= traces.MAX_RELEVANT_SPANS
    starts = [t["start"] for t in r["traces"]]
    assert starts == sorted(starts, reverse=True)
    assert _no_leak(r)


def test_sanitization_removes_flag_spans_from_real_trace(replay):
    raw = load_fixture("jaeger/trace_by_id")["response"]["body"]["data"][0]
    raw_services = {p["serviceName"] for p in raw["processes"].values()}
    assert "flagd" in raw_services  # the recording really contains flagd spans
    replay("jaeger/trace_by_id")
    r = get_traces(trace_id=raw["traceID"])
    t = r["traces"][0]
    assert r["status"] == "ok" and r["returned_traces"] == 1
    assert "flagd" not in t["services"]
    assert t["span_count"] < len(raw["spans"])
    assert len(t["relevant_spans"]) <= traces.MAX_RELEVANT_SPANS_SINGLE
    assert all("self_time_ms" in s and s["self_time_ms"] <= s["duration_ms"] for s in t["relevant_spans"])
    assert _no_leak(r)


def test_errors_that_only_came_from_flagd_are_not_reported(replay):
    """fraud-detection's only error spans are flagd EventStream timeouts (recorded)."""
    raw = load_fixture("jaeger/traces_fraud_errors")["response"]["body"]["data"]
    assert raw, "recording must contain error traces"
    replay("jaeger/traces_fraud_errors")
    r = get_traces(service="fraud-detection", errors_only=True, minutes=60)
    assert r["status"] == "no_data"
    assert _no_leak(r)


def test_recommendation_flagd_stream_error_is_not_reported(replay):
    replay("jaeger/traces_recommendation_errors")
    r = get_traces(service="recommendation", errors_only=True, minutes=60)
    assert r["status"] == "no_data" and _no_leak(r)


def test_traces_made_only_of_flag_service_traffic_disappear_entirely(replay):
    """The recorded trace is just fraud-detection -> flagd EventStream (client + server span)."""
    raw = load_fixture("jaeger/traces_fraud_errors")["response"]["body"]["data"]
    assert all("flagd" in sp["operationName"] for t in raw for sp in t["spans"])
    replay("jaeger/traces_fraud_errors")
    r = get_traces(service="fraud-detection", minutes=60)  # even without errors_only
    assert r["status"] == "no_data" and _no_leak(r)


def test_load_generator_prompt_noise_is_a_real_error(replay):
    replay("jaeger/traces_loadgen_errors")
    r = get_traces(service="load-generator", errors_only=True, minutes=15, limit=2)
    assert r["status"] == "ok" and r["summary"]["error_trace_count"] == 2
    assert r["summary"]["error_origins"] == {"load-generator :: POST": 2}
    origin = next(s for s in r["traces"][0]["relevant_spans"] if s.get("error_origin"))
    assert "agent" in origin["status_message"] and origin["exception"]["type"]


# --------------------------------------------------------------------------- #
# Fault-state recordings
# --------------------------------------------------------------------------- #
def test_product_catalog_fault_is_diagnosable_but_flag_free(replay):
    raw = json.dumps(load_fixture("jaeger/fault_product_catalog_errors")["response"]["body"])
    assert "productCatalogFailure" in raw and "Feature Flag" in raw  # the recording really leaks
    replay("jaeger/fault_product_catalog_errors")
    r = get_traces(service="product-catalog", errors_only=True, minutes=5, limit=3)
    assert r["status"] == "ok" and _no_leak(r)
    assert "product-catalog :: oteldemo.ProductCatalogService/GetProduct" in r["summary"]["error_origins"]
    origin = next(s for t in r["traces"] for s in t["relevant_spans"]
                  if s.get("error_origin") and s["service"] == "product-catalog")
    assert origin["status_message"] == filters.REDACTED
    assert origin["attributes"]["demo.product.id"] == "OLJCESPC7Z"  # the symptom stays visible


def test_checkout_fault_points_at_payment_and_product_catalog(replay):
    replay("jaeger/fault_checkout_errors")
    r = get_traces(service="checkout", errors_only=True, minutes=5, limit=4)
    assert r["status"] == "ok" and _no_leak(r)
    origins = r["summary"]["error_origins"]
    assert any(k.startswith("payment ::") for k in origins)
    assert set(r["summary"]["failed_services"]) >= {"checkout", "payment"}
    charge = next(s for t in r["traces"] for s in t["relevant_spans"]
                  if s.get("error_origin") and s["service"] == "payment")
    assert charge["status_message"].startswith("Payment request failed. Invalid token.")  # organic text kept


# --------------------------------------------------------------------------- #
# Not found / empty / errors
# --------------------------------------------------------------------------- #
def test_trace_not_found_is_no_data(replay):
    replay("jaeger/trace_not_found")
    r = get_traces(trace_id="0" * 32)
    assert r["status"] == "no_data" and "not found" in r["message"].lower()


def test_unknown_service_is_no_data(replay):
    replay("jaeger/traces_unknown_service")
    assert get_traces(service="does-not-exist")["status"] == "no_data"


def test_jaeger_400_is_bad_query(replay):
    replay("jaeger/raw_missing_service")
    r = get_traces(service="checkout")
    assert r["error"]["type"] == "bad_query"
    assert "service" in r["error"]["message"]


@pytest.mark.parametrize("status, expected", [(503, "unavailable"), (500, "backend_internal"), (403, "http_error")])
def test_jaeger_error_statuses(replay, status, expected):
    replay(status_code=status, body={"data": None, "errors": [{"code": status, "msg": "boom"}]})
    assert get_traces(service="checkout")["error"]["type"] == expected


def test_non_json_response(replay):
    replay(status_code=502, body="<html>Bad Gateway</html>", content_type="text/html")
    assert get_traces(service="checkout")["error"]["type"] == "http_error"


def test_unexpected_payload(replay):
    replay(body={"data": {"not": "a list"}})
    assert get_traces(service="checkout")["error"]["type"] == "invalid_response"


@pytest.mark.parametrize("exc, expected", [(httpx.ReadTimeout("slow"), "timeout"),
                                           (httpx.ConnectError("refused"), "connection")])
def test_transport_failures(failing, exc, expected):
    seen = failing(exc)
    assert get_traces(service="checkout")["error"]["type"] == expected
    assert len(seen) == 1


def test_oversized_response(replay, monkeypatch):
    monkeypatch.setattr(traces, "MAX_RESPONSE_BYTES", 1024)
    replay("jaeger/traces_checkout")
    assert get_traces(service="checkout")["error"]["type"] == "response_too_large"


# --------------------------------------------------------------------------- #
# Validation and refusal
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kwargs", [
    {}, {"service": ""}, {"service": "x" * 101}, {"service": "checkout", "operation": " "},
    {"service": "checkout", "minutes": 0}, {"service": "checkout", "minutes": 10081},
    {"service": "checkout", "limit": 0}, {"service": "checkout", "limit": 51},
    {"service": "checkout", "errors_only": "yes"}, {"service": "checkout", "min_duration_ms": -1},
    {"service": "checkout", "min_duration_ms": True}, {"trace_id": "not-hex"}, {"trace_id": "abc"},
])
def test_invalid_input(replay, kwargs):
    rec = replay("jaeger/traces_checkout")
    assert get_traces(**kwargs)["error"]["type"] == "invalid_input"
    assert rec.requests == []


@pytest.mark.parametrize("kwargs", [
    {"service": "flagd"}, {"service": "flagd-ui"},
    {"service": "cart", "operation": "flagd.evaluation.v2.Service/ResolveBoolean"},
    {"service": "frontend-proxy", "operation": "router flagservice egress"},
])
def test_flag_probing_is_refused_without_a_request(replay, kwargs):
    rec = replay("jaeger/traces_checkout")
    r = get_traces(**kwargs)
    assert r["error"]["type"] == "forbidden_query" and rec.requests == [] and _no_leak(r)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _s(span_id, parent=None, start=0, dur=10, error=False, service="svc", op="op"):
    return {"trace_id": "t" * 32, "span_id": span_id, "parent_id": parent, "service": service, "operation": op,
            "start_us": start, "duration_us": dur, "kind": "server", "error": error, "status_message": None,
            "attributes": {}, "events": []}


def test_error_origin_is_the_deepest_failing_span():
    spans = [_s("a", None, 0, 100, True, "frontend"), _s("b", "a", 10, 50, True, "checkout"),
             _s("c", "b", 20, 10, True, "payment", "charge"), _s("d", "b", 40, 10, False, "email")]
    t = traces._summarise_trace(spans, 5)
    assert t["error_origins"] == [{"service": "payment", "operation": "charge"}]
    assert t["failed_services"] == ["checkout", "frontend", "payment"]
    assert t["relevant_spans"][0]["service"] == "payment" and t["relevant_spans"][0]["error_origin"] is True
    assert t["root_service"] == "frontend" and t["duration_ms"] == 0.1


def test_self_time_uses_the_union_of_overlapping_children():
    parent = _s("p", None, 0, 100)
    kids = [_s("c1", "p", 10, 30), _s("c2", "p", 20, 30), _s("c3", "p", 90, 50)]  # union 10-50 and 90-100
    assert traces._self_time_us(parent, kids) == 100 - 40 - 10


def test_nearest_rank_percentile():
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert traces._percentile(values, 50) == 3.0 and traces._percentile(values, 95) == 100.0
    assert traces._percentile([7.0], 95) == 7.0


def test_partially_arrived_traces_are_marked_incomplete(replay):
    """Recorded seconds after the request: frontend/product-catalog spans had not been exported yet."""
    replay("jaeger/fault_checkout_errors")
    r = get_traces(service="checkout", errors_only=True, minutes=5, limit=8)
    partial = next(t for t in r["traces"] if t["trace_id"].startswith("35b0dbb25364"))
    assert partial["incomplete"] is True
    assert "product-catalog" not in partial["services"]  # not arrived yet, so origins are approximate
    assert r["summary"]["incomplete_trace_count"] >= 1


def test_complete_trace_is_not_marked_incomplete_even_after_flag_spans_are_removed(replay):
    replay("jaeger/trace_by_id")
    t = get_traces(trace_id="a" * 32)["traces"][0]
    assert "incomplete" not in t
