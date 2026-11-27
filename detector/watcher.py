"""Incident detector, version 1: a fixed error-ratio threshold (no AI).

    .venv/bin/python -m detector.watcher             # watch; on an alert the agent team investigates
    .venv/bin/python -m detector.watcher --single    # Week-2 behaviour: one diagnosis agent
    .venv/bin/python -m detector.watcher --dry-run   # only print alerts (no Gemini calls)

Every ``poll`` seconds it reads the error ratio of every service with the
same tested PromQL as ``get_service_health``, through ``query_metrics`` (the
read-only, flag-filtered tool the agent uses), smoothed over 5 minutes:
low-traffic services (checkout sees ~1-3 orders a minute) swing between 0 % and
100 % over shorter windows.
When at least one service stays above ``threshold`` % for ``for`` seconds, it
raises ONE alert for the whole incident (failures cascade, so payment, checkout
and frontend breaching together are one incident, not three), hands the alert
to the agent team (Week 4: the run stops at "awaiting_approval" and prints the
approve command; nothing is changed without a human), then stays quiet for
``cooldown`` seconds.

Version 2 (later): replace the fixed threshold by a learned normal band
(rolling mean +- 3 standard deviations) so latency faults are caught too.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot.tools import query_metrics  # noqa: E402
from copilot.tools.health import SPAN_KINDS, golden_signal_queries  # noqa: E402


@dataclass(frozen=True)
class DetectorConfig:
    threshold_pct: float = 5.0      # error ratio that counts as a breach
    min_rps: float = 0.01           # ignore services with almost no traffic (one failure = 100 %)
    for_seconds: float = 120.0      # breach must last this long (filters one-off blips)
    cooldown_seconds: float = 600.0  # one alert per incident
    rate_window: str = "5m"         # smoothing of the error ratio


@dataclass
class Alert:
    fired_at: float
    breach_started_at: float
    services: dict[str, float]      # service -> error % at fire time, highest first

    def message(self, cfg: DetectorConfig) -> str:
        listing = ", ".join(f"{s} {pct:.1f}%" for s, pct in self.services.items())
        return (
            f"ALERT (automated detector): error ratio above {cfg.threshold_pct:g}% for at least "
            f"{cfg.for_seconds / 60:g} minutes. Breaching services: {listing}. "
            f"Breach started {_hms(self.breach_started_at)} UTC, alert fired {_hms(self.fired_at)} UTC. "
            "Investigate and find the root cause."
        )


@dataclass
class ThresholdDetector:
    """Pure state machine: feed it readings, it tells you when to alert. No I/O, easy to test."""

    cfg: DetectorConfig = field(default_factory=DetectorConfig)
    breach_since: dict[str, float] = field(default_factory=dict)
    last_alert_at: float | None = None

    def observe(self, now: float, readings: dict[str, tuple[float, float]]) -> Alert | None:
        """``readings``: service -> (error_pct, requests_per_sec). Returns an Alert or None."""
        breaching = {s: pct for s, (pct, rps) in readings.items()
                     if pct is not None and pct > self.cfg.threshold_pct and (rps or 0.0) >= self.cfg.min_rps}
        for service in list(self.breach_since):
            if service not in breaching:
                del self.breach_since[service]          # recovered: the clock restarts
        for service in breaching:
            self.breach_since.setdefault(service, now)

        if self.last_alert_at is not None and now - self.last_alert_at < self.cfg.cooldown_seconds:
            return None
        sustained = {s: pct for s, pct in breaching.items() if now - self.breach_since[s] >= self.cfg.for_seconds}
        if not sustained:
            return None
        self.last_alert_at = now
        ordered = dict(sorted(breaching.items(), key=lambda kv: -kv[1]))
        return Alert(fired_at=now, breach_started_at=min(self.breach_since[s] for s in sustained), services=ordered)


def read_error_ratios(cfg: DetectorConfig, query=query_metrics) -> dict[str, Any]:
    """{"status": "ok", "readings": {service: (error_pct, requests_per_sec)}} or a status=error dict."""
    queries = golden_signal_queries(SPAN_KINDS, "service_name", rate_window=cfg.rate_window)
    latest: dict[str, dict[str, float]] = {}
    for signal in ("error_pct", "requests_per_sec"):
        res = query(queries[signal], 5, max_series=200)
        if res.get("status") == "error":
            return res
        for series in res.get("series") or []:
            name = (series.get("labels") or {}).get("service_name")
            if name and series.get("latest") is not None:
                latest.setdefault(name, {})[signal] = float(series["latest"])
    readings = {s: (v["error_pct"], v.get("requests_per_sec", 0.0)) for s, v in latest.items() if "error_pct" in v}
    return {"status": "ok", "readings": readings}


def watch(cfg: DetectorConfig, poll_seconds: float, on_alert: Callable[[Alert], None], *,
          once: bool = False, clock: Callable[[], float] = time.time,
          sleep: Callable[[float], None] = time.sleep, reader=None) -> None:
    detector = ThresholdDetector(cfg)
    reader = reader or (lambda: read_error_ratios(cfg))
    while True:
        result = reader()
        now = clock()
        if result.get("status") == "error":
            print(f"{_hms(now)}  metrics query failed: {result.get('error')}", flush=True)
        else:
            readings = result["readings"]
            hot = {s: round(v[0], 1) for s, v in readings.items() if v[0] > cfg.threshold_pct}
            print(f"{_hms(now)}  services above {cfg.threshold_pct:g}%: {hot or 'none'}", flush=True)
            alert = detector.observe(now, readings)
            if alert:
                on_alert(alert)
        if once:
            return
        sleep(poll_seconds)


def _hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")


def main() -> int:
    p = argparse.ArgumentParser(description="Error-ratio incident detector (version 1).")
    p.add_argument("--threshold", type=float, default=5.0, help="error ratio %% that counts as a breach")
    p.add_argument("--for", dest="for_seconds", type=float, default=120.0, help="seconds a breach must last")
    p.add_argument("--cooldown", type=float, default=600.0, help="seconds of silence after an alert")
    p.add_argument("--poll", type=float, default=15.0, help="seconds between checks")
    p.add_argument("--dry-run", action="store_true", help="print alerts but do not start the agent")
    p.add_argument("--single", action="store_true", help="use the Week-2 single diagnosis agent")
    p.add_argument("--once", action="store_true", help="check once and exit")
    args = p.parse_args()
    cfg = DetectorConfig(threshold_pct=args.threshold, for_seconds=args.for_seconds, cooldown_seconds=args.cooldown)

    def on_alert(alert: Alert) -> None:
        text = alert.message(cfg)
        print(f"\n{'!' * 78}\n{text}\n{'!' * 78}", flush=True)
        if args.dry_run:
            return
        if not args.single:
            _run_team(text, alert)
            return
        from copilot.agents.console import print_step, save_run
        from copilot.agents.diagnosis import diagnose
        run = diagnose(text, on_step=print_step)
        path = save_run({**run.to_dict(), "detector": {"fired_at": alert.fired_at,
                                                        "breach_started_at": alert.breach_started_at,
                                                        "services": alert.services}}, prefix="incident")
        print(f"\nagent finished: {run.tool_calls} tool calls, {run.tokens['total']} tokens, "
              f"{run.latency_seconds:.1f} s, error={run.error}  -> {path}\n", flush=True)

    if not args.dry_run:
        import os
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        if not os.environ.get("GOOGLE_API_KEY"):
            print("GOOGLE_API_KEY is not set (.env). Use --dry-run to watch without the agent.", file=sys.stderr)
            return 2

    print(f"watching: error ratio > {cfg.threshold_pct:g}% for {cfg.for_seconds:g}s, poll {args.poll:g}s, "
          f"cooldown {cfg.cooldown_seconds:g}s{' (dry run)' if args.dry_run else ''}", flush=True)
    try:
        watch(cfg, args.poll, on_alert, once=args.once)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def _run_team(text: str, alert: Alert) -> None:
    """Week 4: the agent team investigates; a proposed action waits for a human."""
    import asyncio

    from copilot.agents.console import format_incident, print_team_step, save_run
    from copilot.agents.coordinator import IncidentCoordinator

    async def investigate():
        coordinator = IncidentCoordinator(on_step=print_team_step)
        try:
            return await coordinator.investigate(text)
        finally:
            await coordinator.close()

    try:
        record = asyncio.run(investigate())
    except Exception as exc:  # the watcher must keep watching even if one investigation cannot start
        print(f"\nthe agent team could not run: {type(exc).__name__}: {exc}\n", flush=True)
        return
    data = record.model_dump(mode="json")
    path = save_run({**data, "detector": {"fired_at": alert.fired_at, "breach_started_at": alert.breach_started_at,
                                          "services": alert.services}}, prefix=f"incident-{record.id}")
    print(f"\n{format_incident(data)}\n  saved:      {path}", flush=True)
    if record.status == "awaiting_approval":
        print(f"  decide:     .venv/bin/python scripts/incident.py approve {record.id}   (or reject)\n", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
