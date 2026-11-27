"""Did the fix work? (Week 4) Poll the error ratio after an approved action.

    await verify_recovery(["payment", "checkout"], failure_type="errors", minutes=8)

Recovered = every listed service is below ``threshold_pct`` in two polls in a row. It reads
the same golden-signal tool as the agents (``get_service_health``), so it sees exactly what
they saw. Only error-type faults can be verified this way: for latency, memory or queue
faults the error ratio stays near 0 % even while the problem continues, so a check would
claim success falsely. For those, the result says that a human has to check.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from copilot.state import now_iso
from copilot.tools import get_service_health

__all__ = ["VERIFIABLE_FAILURE_TYPES", "verify_recovery"]

VERIFIABLE_FAILURE_TYPES = frozenset({"errors", "unavailable"})


async def verify_recovery(services: list[str], *, failure_type: str, minutes: float, poll_seconds: float = 30.0,
                          threshold_pct: float = 5.0, required_ok_polls: int = 2,
                          health: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Poll until recovered or ``minutes`` have passed. Never raises. ``health`` defaults to get_service_health."""
    health = health or get_service_health          # looked up at call time, so tests can replace it
    services = sorted({s for s in services if s and s != "none"})
    if failure_type not in VERIFIABLE_FAILURE_TYPES:
        return {"supported": False, "recovered": None,
                "message": f"Automatic verification checks error ratios only; a '{failure_type}' fault needs "
                           "a human to check latency, memory or queue dashboards."}
    if not services:
        return {"supported": False, "recovered": None, "message": "No services to verify."}

    deadline = time.monotonic() + max(0.0, minutes) * 60
    readings: list[dict[str, Any]] = []
    ok_polls = 0
    while True:
        try:
            result = await asyncio.to_thread(health, 5)
        except Exception as exc:  # the tool never raises, but a test double might
            result = {"status": "error", "error": {"message": type(exc).__name__}}
        rows = {r.get("service"): r for r in result.get("services", [])} if result.get("status") == "ok" else {}
        snapshot = {s: ((rows.get(s) or {}).get("error_pct") or {}).get("now") for s in services}
        healthy = all(v is not None and v < threshold_pct for v in snapshot.values())
        readings.append({"at": now_iso(), "error_pct_now": snapshot, "healthy": healthy})
        ok_polls = ok_polls + 1 if healthy else 0
        if ok_polls >= required_ok_polls:
            return {"supported": True, "recovered": True, "services": services, "readings": readings,
                    "message": f"Error ratio below {threshold_pct:g}% for {', '.join(services)} in "
                               f"{required_ok_polls} polls in a row."}
        if time.monotonic() >= deadline:
            return {"supported": True, "recovered": False, "services": services, "readings": readings,
                    "message": f"Not recovered within {minutes:g} minutes."}
        await asyncio.sleep(poll_seconds)
