"""Tests for copilot.tools.logs.search_logs.

Replays responses recorded from the demo's OpenSearch 3.7 (scripts/record_tool_fixtures.py),
including fault-state data recorded while productCatalogFailure, paymentFailure,
adManualGc and intlShippingSlowdown were active.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from copilot.tools import filters, logs
from copilot.tools.logs import build_query, search_logs
from tests.conftest import load_fixture

LEAK = re.compile(r"flag|ofrep|openfeature|" + "|".join(filters.FLAG_KEYS), re.IGNORECASE)


def _no_leak(result) -> bool:
    return not LEAK.search(json.dumps(result))


# --------------------------------------------------------------------------- #
# Query construction
# --------------------------------------------------------------------------- #
def _filters(body):
    return body["query"]["bool"]["filter"]


def test_time_range_uses_observed_timestamp_not_timestamp():
    """@timestamp is 1970-01-01 for shipping (Rust) and currency (C++) logs."""
    body = build_query(1_800_000_000.0, 1_800_000_900.0)
    rng = _filters(body)[0]["range"]
    assert list(rng) == ["observedTimestamp"]
    assert rng["observedTimestamp"]["gte"] == "2027-01-15T08:00:00.000Z"
    assert rng["observedTimestamp"]["lte"] == "2027-01-15T08:15:00.000Z"
    assert body["sort"] == [{"observedTimestamp": {"order": "desc"}}]


def test_filters_for_service_text_and_trace():
    body = build_query(0, 60, service="checkout", text="payment failed", trace_id="AB" * 16)
    flt = _filters(body)
    assert {"term": {"resource.service.name.keyword": "checkout"}} in flt
    assert {"term": {"traceId.keyword": "ab" * 16}} in flt
    assert body["query"]["bool"]["must"] == [{"match": {"body": {"query": "payment failed", "operator": "and"}}}]


def test_level_is_a_minimum_and_matches_number_or_text_only_records():
    clause = _filters(build_query(0, 60, level="warn"))[1]["bool"]["should"]
    assert clause[0] == {"range": {"severity.number": {"gte": 13, "lte": 24}}}
    texts = clause[1]["bool"]["filter"][0]["terms"]["severity.text.keyword"]
    assert {"WARN", "warn", "Warning", "ERROR", "error", "fatal", "Critical"} <= set(texts)
    assert "INFO" not in texts and "info" not in texts


def test_restricted_records_are_excluded_inside_the_query():
    must_not = build_query(0, 60)["query"]["bool"]["must_not"]
    assert {"terms": {"resource.service.name.keyword": ["flagd", "flagd-ui"]}} in must_not
    text_rule = next(c["query_string"] for c in must_not if "fields" in c.get("query_string", {}))
    assert text_rule["fields"] == ["body", "eventName"]
    for term in ("*flagservice*", "*feature_flag*", '"feature flag"', "adManualGc", "intlShippingSlowdown"):
        assert term in text_rule["query"]
    assert {"query_string": {"query": "_exists_:attributes.feature_flag*"}} in must_not


def test_aggregations_requested():
    aggs = build_query(0, 60)["aggs"]
    assert set(aggs) == {"by_service", "by_level", "first_seen", "last_seen"}
    assert set(aggs["by_level"]["filters"]["filters"]) == set(logs.LEVELS)


# --------------------------------------------------------------------------- #
# Request shape (read-only)
# --------------------------------------------------------------------------- #
def test_request_is_a_get_with_json_body_to_the_log_index(replay, monkeypatch):
    monkeypatch.setattr(logs, "_now", lambda: 1_800_000_000.0)
    rec = replay("opensearch/logs_checkout")
    search_logs(service="checkout", level="error", minutes=30, limit=5)
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "GET"
    assert req.url.path == "/otel-logs-*/_search"
    assert dict(req.url.params) == {"ignore_unavailable": "true", "allow_no_indices": "true"}
    assert req.headers["content-type"] == "application/json"
    body = rec.last_json_body
    assert body["size"] == logs.SAMPLE_SIZE and body["timeout"] == "10s" and body["track_total_hits"] is True
    assert rec.base_urls == ["http://localhost:9200"]


def test_env_vars_choose_url_index_and_timeout(replay, monkeypatch):
    monkeypatch.setenv("OPENSEARCH_URL", "http://search.internal:9201/")
    monkeypatch.setenv("OPENSEARCH_LOGS_INDEX", "otel-logs-2026*")
    monkeypatch.setenv("OPENSEARCH_TIMEOUT_SECONDS", "4")
    rec = replay("opensearch/logs_empty")
    search_logs(minutes=5)
    assert rec.base_urls == ["http://search.internal:9201"]
    assert rec.requests[0].url.path == "/otel-logs-2026*/_search"
    assert rec.last_json_body["timeout"] == "4s"


@pytest.mark.parametrize("index", ["_all", "otel-logs-*,secrets", "../x", "Otel"])
def test_invalid_index_pattern_is_a_configuration_error(monkeypatch, index):
    monkeypatch.setenv("OPENSEARCH_LOGS_INDEX", index)
    assert search_logs()["error"]["type"] == "configuration"


def test_bad_url_is_a_configuration_error(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_URL", "localhost:9200")
    assert search_logs()["error"]["type"] == "configuration"


# --------------------------------------------------------------------------- #
# Summaries from recorded responses
# --------------------------------------------------------------------------- #
def test_recent_logs_summary(replay):
    replay("opensearch/logs_recent")
    raw = load_fixture("opensearch/logs_recent")["response"]["body"]
    r = search_logs(minutes=15, limit=20)
    assert r["status"] == "ok"
    assert r["total_matches"] == raw["hits"]["total"]["value"]
    assert len(r["records"]) == 20 and r["truncated"] is True
    assert "frontend-proxy" in r["by_service"]
    assert sum(r["by_level"].values()) == r["total_matches"]  # incl. "unspecified"
    assert r["patterns"] and all(p["count"] >= 1 for p in r["patterns"])
    assert all(len(x["message"]) <= logs.MAX_MESSAGE_CHARS for x in r["records"])
    assert _no_leak(r)


def test_patterns_collapse_variable_parts():
    assert logs._template('[2026-11-20T20:45:00.761Z] "GET /api/products/1YMWWN1N4O HTTP/1.1" 200 - 16') == \
        '"GET /api/products/<id> HTTP/<n>" <n> - <n>'
    assert logs._template("Consumed record with orderId: 2cafd022-e523-11f1-9d5a-7e257ea78f18, total 163") == \
        "Consumed record with orderId: <uuid>, total <n>"


def test_epoch_timestamps_fall_back_to_observed_timestamp():
    rec = logs._normalise({"@timestamp": "1970-01-01T00:00:00Z", "observedTimestamp": "2026-11-20T20:41:26Z",
                           "resource": {"service.name": "shipping"}, "body": "Requesting quote",
                           "severity": {"number": 9, "text": "INFO"}})
    assert rec["time"] == "2026-11-20T20:41:26Z" and rec["service"] == "shipping" and rec["level"] == "info"


@pytest.mark.parametrize("number, text, expected", [
    (9, "Information", "info"), (17, "ERROR", "error"), (None, "INFO", "info"), (None, "Warning", "warn"),
    (None, None, "unspecified"), (13, None, "warn"), (None, "weird", "unspecified"),
])
def test_level_names(number, text, expected):
    assert logs._level_name(number, text) == expected


def test_service_name_read_from_dotted_or_nested_resource():
    assert logs._get({"service.name": "cart"}, "service.name") == "cart"
    assert logs._get({"service": {"name": "cart"}}, "service.name") == "cart"
    assert logs._get({}, "service.name") is None


def test_text_search_fixture(replay):
    replay("opensearch/logs_text")
    r = search_logs(text="Product Found", minutes=15, limit=5)
    assert r["status"] == "ok" and len(r["records"]) == 5
    assert all("Product Found" in x["message"] for x in r["records"])


def test_warn_fixture_contains_only_warn_or_worse(replay):
    replay("opensearch/logs_warn")
    r = search_logs(level="warn", minutes=120)
    assert r["status"] == "ok"
    assert set(r["by_level"]) <= {"warn", "error", "fatal"}
    assert all(x["level"] in ("warn", "error", "fatal") for x in r["records"])


def test_empty_result_is_no_data(replay):
    replay("opensearch/logs_empty")
    r = search_logs(service="does-not-exist", minutes=5)
    assert r["status"] == "no_data" and r["records"] == []


# --------------------------------------------------------------------------- #
# Feature-flag isolation on real data
# --------------------------------------------------------------------------- #
def test_raw_flag_records_never_reach_the_caller(replay):
    """Recorded WITHOUT the server-side exclusion: Envoy /flagservice logs and shipping flag events."""
    raw = json.dumps(load_fixture("opensearch/raw_flag_records")["response"]["body"])
    assert "flagservice" in raw and "feature_flag" in raw  # the recording really leaks
    replay("opensearch/raw_flag_records")
    r = search_logs(minutes=60)
    assert r["status"] == "no_data"
    assert _no_leak(r)


def test_fault_narration_is_dropped_but_symptoms_survive(replay):
    """Recorded during the fault window without exclusions: ad/shipping narration + real errors."""
    raw = json.dumps(load_fixture("opensearch/fault_raw_narration")["response"]["body"])
    assert "Feature Flag adManualGc enabled" in raw and "intlShippingSlowdown feature flag" in raw
    replay("opensearch/fault_raw_narration")
    r = search_logs(minutes=5, limit=100)
    assert _no_leak(r)
    messages = {x["message"] for x in r["records"]}
    assert "Payment request failed. Invalid token. demo.user_context.loyalty_level=gold" in messages
    frontend = [x for x in r["records"] if x["service"] == "frontend" and x["level"] == "error"]
    assert frontend, "organic frontend errors must be kept"
    assert any(x.get("attributes", {}).get("exception.message") == filters.REDACTED for x in frontend)


def test_fault_warn_search_counts_match_records(replay):
    replay("opensearch/fault_logs_warn")
    r = search_logs(level="warn", minutes=5, limit=100)
    assert r["status"] == "ok" and _no_leak(r)
    assert r["sample_size"] == min(r["total_matches"], logs.SAMPLE_SIZE)


@pytest.mark.parametrize("kwargs", [
    {"service": "flagd"}, {"service": "flagd-ui"}, {"text": "feature flag"}, {"text": "adManualGc"},
    {"text": "FeatureFlag"}, {"text": "flagservice"},
])
def test_flag_probing_inputs_are_refused_without_a_request(replay, kwargs):
    rec = replay("opensearch/logs_recent")
    r = search_logs(**kwargs)
    assert r["error"]["type"] == "forbidden_query"
    assert rec.requests == []
    assert _no_leak(r)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def test_opensearch_400_is_bad_query(replay):
    replay("opensearch/raw_bad_request")
    r = search_logs()
    assert r["status"] == "error" and r["error"]["type"] == "bad_query"
    assert r["error"]["message"].startswith("HTTP 400:")
    assert "not-a-date" in r["error"]["message"] or "parse" in r["error"]["message"].lower()


@pytest.mark.parametrize("status, expected", [(429, "unavailable"), (503, "unavailable"), (500, "backend_internal"),
                                              (404, "http_error")])
def test_opensearch_error_statuses(replay, status, expected):
    replay(status_code=status, body={"error": {"type": "x", "reason": "boom"}, "status": status})
    assert search_logs()["error"]["type"] == expected


def test_timed_out_search_is_flagged_as_partial(replay):
    body = load_fixture("opensearch/logs_checkout")["response"]["body"]
    body = {**body, "timed_out": True}
    replay(body=body)
    r = search_logs(service="checkout")
    assert r["status"] == "ok" and any("partial" in w for w in r["warnings"])


def test_non_json_response(replay):
    replay(status_code=502, body="<html>Bad Gateway</html>", content_type="text/html")
    assert search_logs()["error"]["type"] == "http_error"


def test_unexpected_payload(replay):
    replay(body={"acknowledged": True})
    assert search_logs()["error"]["type"] == "invalid_response"


@pytest.mark.parametrize("exc, expected", [(httpx.ReadTimeout("slow"), "timeout"),
                                           (httpx.ConnectError("refused"), "connection")])
def test_transport_failures(failing, exc, expected):
    seen = failing(exc)
    assert search_logs()["error"]["type"] == expected
    assert len(seen) == 1


@pytest.mark.parametrize("kwargs", [
    {"minutes": 0}, {"minutes": 10081}, {"minutes": "5"}, {"limit": 0}, {"limit": 101}, {"limit": True},
    {"level": "verbose"}, {"level": 3}, {"service": ""}, {"service": "x" * 101}, {"text": "  "},
    {"trace_id": "xyz"}, {"trace_id": "a" * 16},
])
def test_invalid_input(replay, kwargs):
    rec = replay("opensearch/logs_recent")
    assert search_logs(**kwargs)["error"]["type"] == "invalid_input"
    assert rec.requests == []


@pytest.mark.parametrize("level", ["WARN", "Warning", "error", "Information", "critical"])
def test_level_aliases_accepted(replay, level):
    replay("opensearch/logs_warn")
    assert search_logs(level=level)["status"] == "ok"
