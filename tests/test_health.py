"""Unit tests for get_service_health / get_service_metrics (no network)."""

from __future__ import annotations

import json

import pytest

from copilot.tools import health, metrics
from copilot.tools.health import get_service_health, get_service_metrics, golden_signal_queries, observations


def _series(label: str, value: str, *, first=None, early=None, recent=None, latest=None, mx=None, by="service_name"):
    latest = latest if latest is not None else recent
    return {"labels": {by: value}, "first": first, "early_median": early, "recent_median": recent,
            "latest": latest, "max": mx if mx is not None else max(x for x in (early, recent) if x is not None)}


def _signal(promql: str) -> str:
    if promql.startswith("histogram_quantile"):
        return "p95_ms"
    if promql.startswith("100 *"):
        return "error_pct"
    return "requests_per_sec"


@pytest.fixture
def fake_metrics(monkeypatch):
    """fake_metrics({signal: [series, ...] | error_dict}) -> list of (promql, minutes) calls."""
    calls: list[tuple[str, int]] = []

    def install(data: dict):
        def fake(promql, minutes, max_series=20):
            calls.append((promql, minutes))
            value = data.get(_signal(promql), [])
            if isinstance(value, dict):
                return value
            return {"status": "ok" if value else "no_data", "series": value}
        monkeypatch.setattr(health, "query_metrics", fake)
        return calls

    return install


PAYMENT_INCIDENT = {
    "requests_per_sec": [_series("s", "payment", early=0.05, recent=0.05),
                         _series("s", "checkout", early=0.05, recent=0.05),
                         _series("s", "cart", early=1.5, recent=1.5)],
    "error_pct": [_series("s", "payment", early=0.0, recent=48.0, mx=60.0),
                  _series("s", "checkout", early=0.0, recent=45.0),
                  _series("s", "cart", early=0.0, recent=0.0)],
    "p95_ms": [_series("s", "payment", early=3.0, recent=3.0),
               _series("s", "checkout", early=150.0, recent=160.0),
               _series("s", "cart", early=2.0, recent=2.0)],
}


# --------------------------------------------------------------------------- #
# get_service_health
# --------------------------------------------------------------------------- #
def test_overview_merges_three_signals_and_sorts_unhealthy_first(fake_metrics):
    calls = fake_metrics(PAYMENT_INCIDENT)
    out = get_service_health(15)
    assert out["status"] == "ok" and len(calls) == 3
    assert [r["service"] for r in out["services"]] == ["payment", "checkout", "cart"]
    assert out["services_with_observations"] == ["payment", "checkout"]
    pay = out["services"][0]
    assert pay["error_pct"] == {"now": 48.0, "window_start": 0.0, "max": 60.0}
    assert pay["observations"] == ["error ratio 48% now vs 0% at window start"]
    assert out["services"][2]["observations"] == []


def test_overview_is_compact(fake_metrics):
    fake_metrics(PAYMENT_INCIDENT)
    assert len(json.dumps(get_service_health(15))) < 2_000


def test_window_is_passed_through(fake_metrics):
    calls = fake_metrics(PAYMENT_INCIDENT)
    get_service_health(30)
    assert {m for _, m in calls} == {30}


@pytest.mark.parametrize("minutes", [0, 4, 361, "15", None, True, 2.5])
def test_invalid_window_is_refused_without_querying(fake_metrics, minutes):
    calls = fake_metrics(PAYMENT_INCIDENT)
    out = get_service_health(minutes)
    assert out["status"] == "error" and out["error"]["type"] == "invalid_input"
    assert calls == []


def test_no_traffic_is_no_data(fake_metrics):
    fake_metrics({})
    assert get_service_health(15)["status"] == "no_data"


def test_all_queries_failing_is_an_error(fake_metrics):
    err = {"status": "error", "error": {"type": "connection", "message": "Prometheus down"}}
    fake_metrics({"requests_per_sec": err, "error_pct": err, "p95_ms": err})
    out = get_service_health(15)
    assert out["status"] == "error" and out["error"]["type"] == "query_failed"
    assert "Prometheus down" in out["error"]["message"]


def test_one_failing_query_becomes_a_warning(fake_metrics):
    data = dict(PAYMENT_INCIDENT, p95_ms={"status": "error", "error": {"type": "timeout", "message": "slow"}})
    fake_metrics(data)
    out = get_service_health(15)
    assert out["status"] == "ok" and out["warnings"] == ["p95_ms: timeout: slow"]
    assert out["services"][0]["p95_ms"] is None


def test_unexpected_payload_never_raises(fake_metrics):
    fake_metrics({"requests_per_sec": [{"labels": {"service_name": "x"}, "max": "not-a-number",
                                        "recent_median": object()}]})
    out = get_service_health(15)
    assert out["status"] in ("ok", "error")


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #
def test_latency_observation_needs_traffic_and_a_real_increase():
    busy = {"requests_per_sec": {"now": 1.0, "window_start": 1.0, "max": 1.0}}
    slow = dict(busy, p95_ms={"now": 900.0, "window_start": 120.0, "max": 950.0})
    assert observations(slow) == ["p95 latency 900 ms now vs 120 ms at window start (7.5x)"]
    tiny = dict(busy, p95_ms={"now": 8.0, "window_start": 2.0, "max": 8.0})          # 4x but only 8 ms
    assert observations(tiny) == []
    quiet = {"requests_per_sec": {"now": 0.02, "window_start": 0.02, "max": 0.03},
             "p95_ms": {"now": 900.0, "window_start": 120.0, "max": 950.0}}            # 1 request a minute
    assert observations(quiet) == []


def test_traffic_spike_and_drop_observations():
    spike = {"requests_per_sec": {"now": 60.0, "window_start": 17.0, "max": 70.0}}
    assert observations(spike) == ["traffic 60 req/s now vs 17 at window start (3.5x)"]
    drop = {"requests_per_sec": {"now": 0.2, "window_start": 5.0, "max": 5.0}}
    assert observations(drop) == ["traffic dropped to 0.2 req/s from 5 at window start"]


def test_past_error_peak_is_reported():
    row = {"error_pct": {"now": 0.0, "window_start": 0.0, "max": 40.0}}
    assert observations(row) == ["error ratio peaked at 40% in the window (now 0%)"]


# --------------------------------------------------------------------------- #
# get_service_metrics
# --------------------------------------------------------------------------- #
def test_service_detail_lists_failing_operation_first(fake_metrics):
    ops = {
        "requests_per_sec": [_series("s", "payment", early=0.05, recent=0.05),
                             _series("s", "charge", early=0.05, recent=0.05, by="span_name"),
                             _series("s", "health", early=1.0, recent=1.0, by="span_name")],
        "error_pct": [_series("s", "payment", early=0.0, recent=50.0),
                      _series("s", "charge", early=0.0, recent=50.0, by="span_name"),
                      _series("s", "health", early=0.0, recent=0.0, by="span_name")],
        "p95_ms": [],
    }
    calls = fake_metrics(ops)
    out = get_service_metrics("payment", 15)
    assert out["status"] == "ok" and len(calls) == 6
    assert all('service_name="payment"' in q for q, _ in calls)
    assert out["service_summary"]["error_pct"]["now"] == 50.0
    assert [o["operation"] for o in out["operations"]] == ["charge", "health"]


def test_service_detail_caps_operations(fake_metrics):
    many = [_series("s", f"op{i}", early=1.0, recent=1.0, by="span_name") for i in range(14)]
    fake_metrics({"requests_per_sec": [_series("s", "cart", early=1.0, recent=1.0)] + many})
    out = get_service_metrics("cart", 15)
    assert len(out["operations"]) == 10 and out["omitted"] == 4


@pytest.mark.parametrize("service", ["", "pay ment", 'a"}', "x" * 80, None, 5, "payment\n"])
def test_invalid_service_is_refused_without_querying(fake_metrics, service):
    calls = fake_metrics(PAYMENT_INCIDENT)
    out = get_service_metrics(service, 15)
    assert out["status"] == "error" and out["error"]["type"] == "invalid_input"
    assert calls == []


@pytest.mark.parametrize("service", ["flagd", "flagd-ui", "paymentFailure"])
def test_flag_service_is_forbidden(fake_metrics, service):
    calls = fake_metrics(PAYMENT_INCIDENT)
    out = get_service_metrics(service, 15)
    assert out["status"] == "error" and out["error"]["type"] == "forbidden_query"
    assert calls == []


def test_unknown_service_is_no_data(fake_metrics):
    fake_metrics({})
    out = get_service_metrics("nosuch", 15)
    assert out["status"] == "no_data" and "operations" not in out


# --------------------------------------------------------------------------- #
# The real PromQL goes through query_metrics' guard and the HTTP layer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("selector", ['span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"',
                                      'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER",service_name="payment"'])
def test_golden_queries_pass_the_metrics_guard(selector):
    for promql in golden_signal_queries(selector, "service_name").values():
        assert metrics._guard_query(promql) is None, promql


def test_end_to_end_through_recorded_prometheus_response(replay):
    body = {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"service_name": "payment"}, "values": [[1, "0"], [61, "0"], [121, "40"], [181, "50"]]},
        {"metric": {"service_name": "flagd"}, "values": [[1, "9"], [61, "9"], [121, "9"], [181, "9"]]},
    ]}}
    rec = replay(body=body)
    out = get_service_health(15)
    assert out["status"] == "ok"
    assert [r["service"] for r in out["services"]] == ["payment"]          # flagd series dropped
    assert len(rec.requests) == 3 and {r.method for r in rec.requests} == {"GET"}
    assert all(r.url.path == "/api/v1/query_range" for r in rec.requests)
