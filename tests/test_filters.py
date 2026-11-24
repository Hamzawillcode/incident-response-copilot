"""Tests for copilot.tools.filters (telemetry sanitization).

Positive cases use strings and shapes taken from the OpenTelemetry Demo 3.1.0
source code and from recorded traces/logs (tests/fixtures).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from copilot.tools import filters
from copilot.tools.filters import REDACTED

DEMO_FLAGS = Path(__file__).resolve().parent.parent / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"


@pytest.mark.skipif(not DEMO_FLAGS.exists(), reason="demo checkout not present")
def test_flag_keys_match_the_demo_flag_file():
    keys = set(json.loads(DEMO_FLAGS.read_text())["flags"])
    assert set(filters.FLAG_KEYS) == keys


# --------------------------------------------------------------------------- #
# Text predicates
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "Error: Product Catalog Fail Feature Flag Enabled",                       # product-catalog/main.go
    "Feature Flag adManualGc enabled, performing a manual gc now",            # ad/AdService.java
    "Delaying international shipment due to intlShippingSlowdown feature flag",  # shipping_service.rs
    "Warning: FeatureFlag 'kafkaQueueProblems' is activated, overloading queue now.",  # checkout/main.go
    "FeatureFlag 'kafkaQueueProblems' is enabled, sleeping 1 second",         # fraud-detection main.kt
    '[2026-11-20T19:48:19.923Z] "POST /flagservice/flagd.evaluation.v1.Service/EventStream HTTP/1.1" 200',
    "shipping.feature_flag.evaluated",
    "flagd.evaluation.v2.Service/ResolveBoolean",
    "http://flagd:8016/ofrep/v1/evaluate/flags/paymentFailure",
    "opentelemetry-demo/flagd-ui",
    "cartFailure",
    "OpenFeature hook",
])
def test_mentions_flags_positive(text):
    assert filters.mentions_flags(text)


@pytest.mark.parametrize("text", [
    "Payment request failed. Invalid token. demo.user_context.loyalty_level=gold",  # organic symptom
    "Triggering a manual garbage collection, next one in 10 seconds.",
    "The artificially triggered GCs took: 1016 ms",
    "Checkout failed to place order",
    "13 INTERNAL: failed to prepare order: failed to get product #\"OLJCESPC7Z\"",
    "flags of the world",  # 'flag' alone is not enough
    "",
])
def test_mentions_flags_negative(text):
    assert not filters.mentions_flags(text)


def test_mentions_flags_ignores_non_strings():
    assert not filters.mentions_flags(None) and not filters.mentions_flags(42)


@pytest.mark.parametrize("name, expected", [
    ("feature_flag.key", True), ("feature_flag.result.variant", True), ("demo.feature_flag.recommendation_cache", True),
    ("shipping.feature_flag.evaluated", True), ("feature_flag_key", True), ("feature_flag_result_variant", True),
    ("http.request.method", False), ("demo.product.id", False), ("exception.message", False),
])
def test_is_flag_name(name, expected):
    assert filters.is_flag_name(name) is expected


@pytest.mark.parametrize("name, expected", [
    ("flagd", True), ("flagd-ui", True), ("FlagD-UI", True), (" flagd ", True),
    ("frontend", False), ("frontend-proxy", False), (None, False),
])
def test_is_restricted_service(name, expected):
    assert filters.is_restricted_service(name) is expected


@pytest.mark.parametrize("values, refused", [
    (("checkout", None), False), (("flagd",), True), (("frontend", "Feature Flag"), True),
    ((None, "adManualGc"), True), (("payment failed",), False),
])
def test_refuse_inputs(values, refused):
    assert (filters.refuse_inputs(*values) is not None) is refused


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_redact_text_whole_string_when_flag_text_remains():
    assert filters.redact_text("Delaying international shipment due to intlShippingSlowdown feature flag") == REDACTED


def test_redact_text_keeps_clean_text_and_non_strings():
    assert filters.redact_text("Payment request failed.") == "Payment request failed."
    assert filters.redact_text(None) is None


def test_redact_text_replaces_only_flag_label_sets():
    msg = 'found duplicate series for {job="opentelemetry-demo/flagd-ui", x="1"} vs {job="opentelemetry-demo/cart"}'
    assert filters.redact_text(msg) == 'found duplicate series for {[redacted]} vs {job="opentelemetry-demo/cart"}'


def test_sanitize_attributes_drops_flag_keys_and_redacts_values_at_any_depth():
    attrs = {
        "feature_flag.key": "cartFailure",
        "demo.feature_flag.recommendation_cache": True,
        "exception.message": "13 INTERNAL: Error: Product Catalog Fail Feature Flag Enabled",
        "nested": {"feature_flag_variant": "on", "note": "paymentFailure", "ok": 1},
        "list": ["fine", "adHighCpu"],
        "http.response.status_code": 500,
    }
    assert filters.sanitize_attributes(attrs) == {
        "exception.message": REDACTED,
        "nested": {"note": REDACTED, "ok": 1},
        "list": ["fine", REDACTED],
        "http.response.status_code": 500,
    }


def test_labels_are_restricted():
    assert filters.labels_are_restricted({"service_name": "flagd"})
    assert filters.labels_are_restricted({"feature_flag_key": "x"})
    assert filters.labels_are_restricted({"span_name": "router flagservice egress"})
    assert not filters.labels_are_restricted({"service_name": "checkout", "le": "+Inf"})


# --------------------------------------------------------------------------- #
# Spans
# --------------------------------------------------------------------------- #
def _span(**kw):
    base = {"service": "checkout", "operation": "oteldemo.CheckoutService/PlaceOrder", "attributes": {},
            "events": [], "status_message": None, "error": False}
    base.update(kw)
    return base


@pytest.mark.parametrize("span", [
    _span(service="flagd", operation="resolveFloat"),
    _span(service="flagd-ui", operation="FlagdUiWeb.Dashboard.mount"),
    _span(service="cart", operation="flagd.evaluation.v2.Service/ResolveFloat"),
    _span(service="cart", operation="POST", attributes={"server.address": "flagd", "server.port": 8013}),
    _span(service="frontend-proxy", operation="router flagservice egress"),
    _span(service="load-generator", operation="POST", attributes={"url.full": "http://flagd:8016/ofrep/v1/evaluate/flags"}),
    _span(service="recommendation", operation="x", attributes={"rpc.service": "flagd.evaluation.v2.Service"}),
    _span(service="x", operation="Feature Flag evaluation"),
])
def test_spans_from_or_to_the_flag_service_are_dropped(span):
    assert filters.sanitize_span(span) is None


def test_product_catalog_fault_span_is_kept_but_stripped():
    """Shape of the real productCatalogFailure span (see fixtures/jaeger/fault_product_catalog_errors)."""
    span = _span(
        service="product-catalog", operation="oteldemo.ProductCatalogService/GetProduct", error=True,
        status_message="Error: Product Catalog Fail Feature Flag Enabled",
        attributes={"demo.product.id": "OLJCESPC7Z", "rpc.grpc.status_code": 13},
        events=[
            {"name": "feature_flag.evaluation", "attributes": {"feature_flag.key": "productCatalogFailure",
                                                               "feature_flag.result.variant": "on"}},
            {"name": "Error: Product Catalog Fail Feature Flag Enabled", "attributes": {}},
            {"name": "exception", "attributes": {"exception.message": "boom"}},
        ],
    )
    clean = filters.sanitize_span(span)
    assert clean is not None and clean["error"] is True
    assert clean["status_message"] == REDACTED
    assert clean["attributes"] == {"demo.product.id": "OLJCESPC7Z", "rpc.grpc.status_code": 13}
    assert [e["name"] for e in clean["events"]] == ["exception"]
    assert "flag" not in json.dumps(clean).lower()


def test_recommendation_flag_attribute_is_removed_from_real_work_span():
    span = _span(service="recommendation", operation="get_product_list",
                 attributes={"demo.feature_flag.recommendation_cache": True, "demo.recommendation.cache_hit": False})
    assert filters.sanitize_span(span)["attributes"] == {"demo.recommendation.cache_hit": False}


def test_sanitize_span_does_not_mutate_its_input():
    span = _span(attributes={"feature_flag.key": "x"}, events=[{"name": "feature_flag.evaluation", "attributes": {}}])
    snapshot = json.dumps(span, sort_keys=True)
    filters.sanitize_span(span)
    assert json.dumps(span, sort_keys=True) == snapshot


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #
def _log(**kw):
    base = {"service": "frontend", "body": "API request failed", "event_name": None, "attributes": {}}
    base.update(kw)
    return base


@pytest.mark.parametrize("record", [
    _log(service="flagd"),
    _log(service="ad", body="Feature Flag adManualGc enabled, performing a manual gc now"),
    _log(service="shipping", body="Delaying international shipment due to intlShippingSlowdown feature flag"),
    _log(service="shipping", body="", event_name="shipping.feature_flag.evaluated",
         attributes={"feature_flag_key": "intlShippingSlowdown", "feature_flag_variant": "off"}),
    _log(service="frontend-proxy", body='"POST /flagservice/flagd.evaluation.v1.Service/EventStream HTTP/1.1" 200',
         attributes={"url.path": "/flagservice/flagd.evaluation.v1.Service/EventStream"}),
    _log(service="checkout", body="Warning: FeatureFlag 'kafkaQueueProblems' is activated, overloading queue now."),
    _log(attributes={"nested": {"feature_flag.key": "x"}}),
])
def test_flag_narration_and_flag_traffic_logs_are_dropped(record):
    assert filters.sanitize_log(record) is None


def test_organic_error_log_carrying_flag_text_is_kept_with_value_redacted():
    """Real frontend record under productCatalogFailure (see fixtures/opensearch/fault_raw_narration)."""
    record = _log(attributes={
        "url.path": "/api/recommendations", "http.response.status_code": 500,
        "exception.message": "13 INTERNAL: Error: Product Catalog Fail Feature Flag Enabled",
        "exception.stacktrace": "Error: 13 INTERNAL: Error: Product Catalog Fail Feature Flag Enabled\n    at ...",
    })
    clean = filters.sanitize_log(record)
    assert clean["body"] == "API request failed"
    assert clean["attributes"] == {"url.path": "/api/recommendations", "http.response.status_code": 500,
                                   "exception.message": REDACTED, "exception.stacktrace": REDACTED}


def test_ordinary_log_is_unchanged():
    record = _log(service="payment", body="Transaction complete.", attributes={"lastFourDigits": "8031"})
    assert filters.sanitize_log(record) == record


# --------------------------------------------------------------------------- #
# Final safety net
# --------------------------------------------------------------------------- #
def test_scrub_output_removes_flag_keys_and_redacts_strings_recursively():
    out = filters.scrub_output({
        "status": "ok",
        "labels": {"feature_flag_key": "cartFailure", "service_name": "cart"},
        "series": [{"note": "flagd restarted", "values": (1, "paymentFailure")}],
        "count": 3,
    })
    assert out == {"status": "ok", "labels": {"service_name": "cart"},
                   "series": [{"note": REDACTED, "values": (1, REDACTED)}], "count": 3}
