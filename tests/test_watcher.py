"""Unit tests for the threshold detector (pure logic, no network, no LLM)."""

from __future__ import annotations

from detector.watcher import Alert, DetectorConfig, ThresholdDetector, read_error_ratios, watch

CFG = DetectorConfig(threshold_pct=5.0, min_rps=0.01, for_seconds=120, cooldown_seconds=600)
BAD = {"payment": (50.0, 0.05), "checkout": (45.0, 0.05), "cart": (0.0, 1.5)}
OK = {"payment": (0.0, 0.05), "checkout": (0.0, 0.05), "cart": (0.0, 1.5)}


def feed(det: ThresholdDetector, readings_by_time: list[tuple[float, dict]]) -> list[Alert]:
    return [a for t, r in readings_by_time if (a := det.observe(t, r))]


def test_no_alert_until_breach_lasts_two_minutes():
    det = ThresholdDetector(CFG)
    alerts = feed(det, [(0, BAD), (60, BAD), (119, BAD)])
    assert alerts == []
    alert = det.observe(120, BAD)
    assert alert and alert.breach_started_at == 0 and alert.fired_at == 120


def test_one_alert_per_incident_with_all_breaching_services_highest_first():
    det = ThresholdDetector(CFG)
    alerts = feed(det, [(t, BAD) for t in range(0, 300, 15)])
    assert len(alerts) == 1
    assert list(alerts[0].services) == ["payment", "checkout"]


def test_a_short_blip_does_not_alert():
    det = ThresholdDetector(CFG)
    assert feed(det, [(0, BAD), (60, BAD), (90, OK), (150, BAD), (200, BAD)]) == []   # clock restarted at 150


def test_cooldown_then_alert_again():
    det = ThresholdDetector(CFG)
    first = feed(det, [(t, BAD) for t in range(0, 700, 30)])
    assert len(first) == 1 and first[0].fired_at == 120
    assert det.observe(720, BAD) is not None                                          # cooldown over


def test_services_without_traffic_are_ignored():
    det = ThresholdDetector(CFG)
    idle = {"email": (100.0, 0.0)}                                                    # 1 failure, 0 rps
    assert feed(det, [(0, idle), (200, idle)]) == []


def test_threshold_is_strictly_greater():
    det = ThresholdDetector(CFG)
    edge = {"payment": (5.0, 1.0)}
    assert feed(det, [(0, edge), (200, edge)]) == []


def test_alert_message_is_plain_and_names_symptoms_only():
    alert = Alert(fired_at=120, breach_started_at=0, services={"payment": 50.0, "checkout": 45.0})
    msg = alert.message(CFG)
    assert "payment 50.0%" in msg and "checkout 45.0%" in msg and "above 5%" in msg and "2 minutes" in msg
    assert "flag" not in msg.lower()


def test_read_error_ratios_uses_smoothed_queries_and_latest_values():
    seen = []

    def query(promql, minutes, max_series=20):
        seen.append(promql)
        if promql.startswith("100 *"):
            return {"status": "ok", "series": [{"labels": {"service_name": "payment"}, "latest": 50.0}]}
        return {"status": "ok", "series": [{"labels": {"service_name": "payment"}, "latest": 0.05},
                                           {"labels": {"service_name": "cart"}, "latest": 1.0}]}

    out = read_error_ratios(CFG, query=query)
    assert out == {"status": "ok", "readings": {"payment": (50.0, 0.05)}}
    assert len(seen) == 2 and all("[5m]" in q and "[2m]" not in q for q in seen)


def test_read_error_ratios_passes_errors_through():
    err = {"status": "error", "error": {"type": "connection", "message": "down"}}
    assert read_error_ratios(CFG, query=lambda *a, **k: err) == err


def test_watch_loop_calls_on_alert_once():
    clock = iter(range(0, 10_000, 30))
    seen: list[Alert] = []
    reader = lambda: {"status": "ok", "readings": {"payment": (50.0, 0.05)}}           # noqa: E731
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 10:
            raise KeyboardInterrupt

    try:
        watch(CFG, 15, seen.append, clock=lambda: next(clock), sleep=sleep, reader=reader)
    except KeyboardInterrupt:
        pass
    assert len(seen) == 1 and seen[0].services == {"payment": 50.0}


def test_watch_survives_health_errors():
    seen: list[Alert] = []
    watch(CFG, 15, seen.append, once=True, clock=lambda: 0.0,
          reader=lambda: {"status": "error", "error": {"type": "connection"}})
    assert seen == []
