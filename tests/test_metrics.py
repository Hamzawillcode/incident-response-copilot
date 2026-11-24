"""Tests for copilot.tools.metrics.query_metrics.

Most tests replay real responses recorded from the OpenTelemetry Demo's
Prometheus 3.13.1 (see scripts/record_prometheus_fixtures.py and scripts/record_tool_fixtures.py). Synthetic data is
used only where a recording cannot express the case (transport failures,
precise trend shapes, oversized bodies).
"""

from __future__ import annotations

import json
import math

import httpx
import pytest

from copilot.tools import _http, metrics
from copilot.tools.metrics import query_metrics
from tests.conftest import load_fixture

RATE_QUERY = "sum by (service_name) (rate(traces_span_metrics_calls_total[5m]))"
FLAG_WORDS = ("flag", "openfeature")


def _no_flag_text(result: dict) -> bool:
    text = json.dumps(result).lower()
    return not any(word in text for word in FLAG_WORDS)


# --------------------------------------------------------------------------- #
# Request shape and read-only behaviour
# --------------------------------------------------------------------------- #
def test_sends_single_get_to_query_range_with_expected_params(replay, monkeypatch):
    monkeypatch.setattr(metrics, "_now", lambda: 1_800_000_000.0)
    rec = replay("prometheus/request_rate_by_service")

    query_metrics(RATE_QUERY, 30)

    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/query_range"
    p = req.url.params
    assert p["query"] == RATE_QUERY
    assert float(p["end"]) == 1_800_000_000.0
    assert float(p["end"]) - float(p["start"]) == 30 * 60
    assert p["step"] == "60"
    assert p["timeout"] == "10s"


@pytest.mark.parametrize("minutes, expected_step", [(1, 60), (60, 60), (240, 60), (1440, 360), (10080, 2520)])
def test_step_keeps_points_bounded(minutes, expected_step):
    assert metrics._step_seconds(minutes) == expected_step
    assert minutes * 60 / expected_step <= metrics.MAX_POINTS_PER_SERIES


def test_min_step_env_var(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_MIN_STEP_SECONDS", "15")
    assert metrics._step_seconds(30) == 15
    for bad in ("0", "-5", "abc", "99999"):
        monkeypatch.setenv("PROMETHEUS_MIN_STEP_SECONDS", bad)
        assert metrics._step_seconds(30) == metrics.DEFAULT_MIN_STEP_SECONDS


def test_get_helper_refuses_paths_outside_allow_list(replay):
    rec = replay("prometheus/empty_result")
    for path in ("/-/quit", "/-/reload", "/api/v1/admin/tsdb/delete_series", "/api/v1/query"):
        with pytest.raises(_http.ToolError) as exc:
            _http.get_json(base_url="http://prometheus.test:9090", path=path, allowed=metrics._ALLOWED, timeout=1)
        assert exc.value.type == "internal"
    assert rec.requests == []


@pytest.mark.allow_network
def test_http_client_ignores_proxy_environment(monkeypatch):
    """The college proxy in HTTP(S)_PROXY/ALL_PROXY must never carry observability traffic."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("PROMETHEUS_URL", "http://127.0.0.1:9")  # discard port: nothing listens
    monkeypatch.setenv("PROMETHEUS_TIMEOUT_SECONDS", "2")
    result = query_metrics(RATE_QUERY, 5)
    assert result["status"] == "error"
    assert result["error"]["type"] == "connection"
    assert "127.0.0.1:9" in result["error"]["message"]  # it tried Prometheus directly, not the proxy


# --------------------------------------------------------------------------- #
# Summaries from recorded responses
# --------------------------------------------------------------------------- #
def test_summarises_real_multiseries_response(replay):
    replay("prometheus/request_rate_by_service")
    raw = load_fixture("prometheus/request_rate_by_service")["response"]["body"]["data"]["result"]

    result = query_metrics(RATE_QUERY, 30)

    assert result["status"] == "ok"
    assert result["series_count"] == len(raw) - 2  # flagd and flagd-ui removed
    names = [s["labels"]["service_name"] for s in result["series"]]
    assert "frontend-proxy" in names and "checkout" in names
    latests = [s["latest"] for s in result["series"]]
    assert latests == sorted(latests, reverse=True)
    for s in result["series"]:
        assert s["min"] <= s["latest"] <= s["max"]
        assert s["min"] <= s["mean"] <= s["max"]
        assert s["trend"] in {"increasing", "decreasing", "flat", "insufficient_data"}
        assert s["points"] > 0
    assert "values" not in json.dumps(result)  # raw samples are never returned


def test_summary_values_match_recorded_samples(replay):
    replay("prometheus/checkout_p95_latency")
    raw = load_fixture("prometheus/checkout_p95_latency")["response"]["body"]["data"]["result"][0]["values"]
    values = [float(v) for _, v in raw if math.isfinite(float(v))]

    s = query_metrics("histogram_quantile(0.95, sum by (le) (rate(x_bucket[5m])))", 30)["series"][0]

    assert s["labels"] == {}
    assert s["min"] == pytest.approx(min(values), rel=1e-5)
    assert s["max"] == pytest.approx(max(values), rel=1e-5)
    assert s["latest"] == pytest.approx(values[-1], rel=1e-5)
    assert s["first"] == pytest.approx(values[0], rel=1e-5)
    assert s["points"] == len(values)


def test_gauge_series_keeps_identifying_labels_only(replay):
    replay("prometheus/frontend_memory_usage")
    s = query_metrics('container_memory_usage_total_bytes{container_name="frontend"}', 30)["series"][0]
    assert s["labels"] == {"container_name": "frontend", "metric": "container_memory_usage_total_bytes"}
    assert s["latest"] > 10 * 1024 * 1024  # a Next.js container uses tens of MiB


def test_many_series_are_truncated_and_flag_container_removed(replay):
    replay("prometheus/all_container_memory")
    result = query_metrics("container_memory_usage_total_bytes", 30, max_series=5)
    assert result["status"] == "ok"
    assert result["series_count"] == 26  # 28 containers minus flagd and flagd-ui
    assert result["returned_series"] == 5 and result["truncated"] is True
    assert len(result["series"]) == 5
    assert _no_flag_text(result)


def test_empty_result_is_no_data_not_error(replay):
    replay("prometheus/empty_result")
    result = query_metrics('traces_span_metrics_calls_total{service_name="does-not-exist"}', 30)
    assert result["status"] == "no_data"
    assert result["series"] == [] and result["series_count"] == 0


def test_nan_samples_are_counted_not_summarised(replay):
    replay("prometheus/nan_values")
    s = query_metrics("vector(0) / vector(0)", 30)
    series = s["series"][0]
    assert s["status"] == "ok"
    assert series["points"] == 0
    assert series["non_finite_points"] > 0
    assert series["latest"] is None and series["trend"] == "insufficient_data"
    json.dumps(s, allow_nan=False)  # still valid strict JSON


def test_prometheus_info_annotations_are_passed_through(replay):
    replay("prometheus/rate_on_gauge_warning")
    result = query_metrics('rate(system_cpu_utilization_ratio{state="user"}[5m])', 30)
    assert result["status"] == "ok"
    assert any("might not be a counter" in w for w in result["warnings"])


# --------------------------------------------------------------------------- #
# Prometheus errors (recorded)
# --------------------------------------------------------------------------- #
def test_syntax_error_is_reported_as_bad_query(replay):
    replay("prometheus/bad_syntax")
    result = query_metrics("sum(rate(traces_span_metrics_calls_total[5m])", 30)
    assert result["status"] == "error"
    assert result["error"]["type"] == "bad_query"
    assert "unclosed left parenthesis" in result["error"]["message"]
    assert "HTTP 400" in result["error"]["message"]


def test_execution_error_message_is_redacted(replay):
    """The real 422 body names job="opentelemetry-demo/flagd-ui" - it must not reach the agent."""
    raw = load_fixture("prometheus/execution_error")["response"]["body"]["error"]
    assert "flagd" in raw  # the recording really does leak

    replay("prometheus/execution_error")
    result = query_metrics("traces_span_metrics_calls_total + on(job) traces_span_metrics_calls_total", 30,
                           )
    assert result["error"]["type"] == "query_execution"
    assert "found duplicate series" in result["error"]["message"]
    assert _no_flag_text(result)
    assert len(result["error"]["message"]) <= 320


@pytest.mark.parametrize(
    "status_code, error_type, expected",
    [(503, "timeout", "timeout"), (503, "unavailable", "unavailable"), (500, "internal", "prometheus_internal"),
     (422, "execution", "query_execution"), (418, "something_new", "http_error")],
)
def test_prometheus_error_types_are_mapped(replay, status_code, error_type, expected):
    replay(status_code=status_code,
                       body={"status": "error", "errorType": error_type, "error": "boom"})
    result = query_metrics(RATE_QUERY, 5)
    assert result["error"]["type"] == expected


def test_non_json_response_from_a_proxy_is_handled(replay):
    replay(status_code=502, body="<html><body>Bad Gateway</body></html>", content_type="text/html")
    result = query_metrics(RATE_QUERY, 5)
    assert result["error"]["type"] == "http_error"
    assert "502" in result["error"]["message"]


def test_success_with_unexpected_shape_is_invalid_response(replay):
    replay(body={"status": "success", "data": {"resultType": "vector", "result": []}})
    assert query_metrics(RATE_QUERY, 5)["error"]["type"] == "invalid_response"


def test_oversized_response_is_rejected(replay, monkeypatch):
    monkeypatch.setattr(metrics, "MAX_RESPONSE_BYTES", 1_000)
    replay("prometheus/request_rate_by_service")  # ~170 KB recording
    assert query_metrics(RATE_QUERY, 30)["error"]["type"] == "response_too_large"


# --------------------------------------------------------------------------- #
# Transport failures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")])
def test_timeouts_are_reported(failing, exc):
    seen = failing(exc)
    result = query_metrics(RATE_QUERY, 5)
    assert result["status"] == "error" and result["error"]["type"] == "timeout"
    assert len(seen) == 1  # no retries hammering a struggling Prometheus


def test_connection_errors_are_reported(failing):
    failing(httpx.ConnectError("refused"))
    assert query_metrics(RATE_QUERY, 5)["error"]["type"] == "connection"


# --------------------------------------------------------------------------- #
# Input validation and configuration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "promql, minutes",
    [("", 5), ("   ", 5), (None, 5), (42, 5), ("up", 0), ("up", -1), ("up", 10081), ("up", 5.0),
     ("up", "5"), ("up", True), ("x" * 2001, 5)],
)
def test_invalid_input_is_rejected_without_network(replay, promql, minutes):
    rec = replay("prometheus/empty_result")
    result = query_metrics(promql, minutes)
    assert result["status"] == "error" and result["error"]["type"] == "invalid_input"
    assert rec.requests == []


def test_invalid_max_series_is_rejected(replay):
    replay("prometheus/empty_result")
    assert query_metrics(RATE_QUERY, 5, max_series=0)["error"]["type"] == "invalid_input"


@pytest.mark.parametrize("url", ["localhost:9090", "ftp://prom:9090", "http://prom:9090/?x=1", "not a url"])
def test_bad_prometheus_url_is_a_configuration_error(monkeypatch, url):
    monkeypatch.setenv("PROMETHEUS_URL", url)
    result = query_metrics(RATE_QUERY, 5)
    assert result["error"]["type"] == "configuration"


def test_timeout_env_var_is_used_and_bounded(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_TIMEOUT_SECONDS", "3.5")
    assert metrics._timeout_seconds() == 3.5
    for bad in ("0", "-1", "999", "abc"):
        monkeypatch.setenv("PROMETHEUS_TIMEOUT_SECONDS", bad)
        assert metrics._timeout_seconds() == metrics.DEFAULT_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# Feature-flag isolation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "promql",
    [
        "sum by (feature_flag_key) (feature_flag_evaluation_requests_total)",
        "feature_flag_evaluation_success_total",
        'rate(traces_span_metrics_calls_total{service_name="flagd"}[5m])',
        'traces_span_metrics_calls_total{job="opentelemetry-demo/FlagD-UI"}',
        'traces_span_metrics_calls_total{span_name=~"router flagservice.*"}',
        'count({__name__=~"feat.+"})',
        'count by (__name__) (traces_span_metrics_calls_total)',
        'count({job="opentelemetry-demo/cart"})',
        'sum( {service_name="cart"} )',
    ],
)
def test_flag_revealing_queries_are_refused_before_any_request(replay, promql):
    rec = replay("prometheus/flag_metric_leak")
    result = query_metrics(promql, 30)
    assert result["status"] == "error" and result["error"]["type"] == "forbidden_query"
    assert rec.requests == []


@pytest.mark.parametrize(
    "promql",
    [
        RATE_QUERY,
        'container_memory_usage_total_bytes{container_name="frontend"}',
        "container_memory_usage_total_bytes {container_name=\"frontend\"}",
        'rate(http_server_request_duration_seconds_count{http_route="/api/{id}"}[5m])',  # brace inside a string
        "histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket[5m])))",
    ],
)
def test_ordinary_queries_pass_the_guard(promql):
    assert metrics._guard_query(promql) is None


def test_flag_series_returned_by_prometheus_are_scrubbed(replay):
    """Even if a query slips past the guard, flag-labelled series never reach the caller."""
    replay("prometheus/flag_metric_leak")  # real series with feature_flag_key labels
    result = query_metrics("sum by (service_name) (some_innocent_looking_metric_total)", 30)
    assert result["status"] == "no_data"
    assert _no_flag_text(result)


def test_redaction_of_prometheus_text():
    # Label sets naming flag series are replaced individually; the rest of the message survives.
    text = 'duplicate series {job="opentelemetry-demo/flagd-ui", x="1"} and {job="cart"}'
    assert metrics._redact(text) == 'duplicate series {[redacted]} and {job="cart"}'
    # If flag text remains outside a label set, the whole message is withheld.
    assert metrics._redact('{job="cart"} via OpenFeature') == "[redacted]"


# --------------------------------------------------------------------------- #
# Trend classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "values, expected",
    [
        ([1, 2, 3, 4, 5, 6], "increasing"),
        ([60, 50, 40, 30, 20, 10], "decreasing"),
        ([100, 101, 99, 100, 101, 100], "flat"),  # noise within 10 %
        ([5, 5, 5, 5], "flat"),
        ([0, 0, 0, 0, 0.02, 0.03, 0.05, 0.05], "increasing"),  # an error rate appearing from zero
        ([10, 30, 10, 30, 10], "flat"),  # oscillation: equal early and recent medians
        ([40] * 19 + [90], "flat"),  # one spike cannot flip the verdict (quarter = 5 points)...
        ([40] * 15 + [90] * 5, "increasing"),  # ...a sustained rise across the last quarter does
        ([7], "insufficient_data"),
    ],
)
def test_trend_classification(values, expected):
    assert metrics._trend([float(v) for v in values])[0] == expected


def test_trend_reports_the_medians_it_compared():
    trend, early, recent = metrics._trend([10.0, 12.0, 11.0, 13.0, 20.0, 22.0, 21.0, 23.0])
    assert (trend, early, recent) == ("increasing", 11.0, 22.0)


@pytest.mark.parametrize("name", ["request_rate_by_service", "checkout_p95_latency", "frontend_memory_usage",
                                  "all_container_memory", "rate_on_gauge_warning"])
def test_trend_is_consistent_with_reported_medians_on_real_data(replay, name):
    """Regression: a whole-window regression once said 'increasing' for a series whose
    medians and endpoints both fell. The verdict must always agree with early/recent medians."""
    replay(f"prometheus/{name}")
    result = query_metrics("recorded_metric_total", 30, max_series=100)
    assert result["status"] == "ok"
    for s in result["series"]:
        if s["trend"] == "increasing":
            assert s["recent_median"] > s["early_median"]
        elif s["trend"] == "decreasing":
            assert s["recent_median"] < s["early_median"]
        elif s["trend"] == "flat" and s["mean"]:
            assert abs(s["recent_median"] - s["early_median"]) <= 0.1 * abs(s["mean"]) * 1.0001


def test_change_percent_is_none_when_series_starts_at_zero():
    s = metrics._summarise({"metric": {}, "values": [[1, "0"], [2, "4"]]}, end=2.0)
    assert s["change"] == 4 and s["change_percent"] is None and s["trend"] == "increasing"


def test_noisy_resource_labels_are_dropped():
    labels = {"__name__": "m", "job": "j", "host_name": "h", "telemetry_sdk_language": "go",
              "service_instance_id": "x", "span_name": "GET /"}
    s = metrics._summarise({"metric": labels, "values": [[1, "1"]]}, end=1.0)
    assert s["labels"] == {"metric": "m", "job": "j", "span_name": "GET /"}
