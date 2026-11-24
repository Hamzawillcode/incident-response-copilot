"""Live integration tests for the health overview tools and the detector (no LLM).

Skipped automatically when Prometheus is not reachable. Run with:  pytest -m live
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from copilot.tools import get_service_health, get_service_metrics
from copilot.tools.metrics import DEFAULT_PROMETHEUS_URL
from detector.watcher import DetectorConfig, read_error_ratios

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


def test_live_overview_covers_the_shop_and_hides_the_flag_service():
    out = get_service_health(15)
    assert out["status"] == "ok", out
    names = {r["service"] for r in out["services"]}
    assert {"frontend", "checkout", "cart", "payment"} <= names
    assert not any("flag" in n for n in names)
    text = json.dumps(out)
    assert "feature_flag" not in text and len(text) < 12_000


def test_live_service_detail():
    out = get_service_metrics("cart", 15)
    assert out["status"] == "ok", out
    assert out["service_summary"]["requests_per_sec"]["now"] > 0
    assert out["operations"] and all("operation" in o for o in out["operations"])


def test_live_detector_readings():
    out = read_error_ratios(DetectorConfig())
    assert out["status"] == "ok", out
    readings = out["readings"]
    assert "frontend" in readings and "flagd" not in readings and all(0 <= pct <= 100 for pct, _ in readings.values())
