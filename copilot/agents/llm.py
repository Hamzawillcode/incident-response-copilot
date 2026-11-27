"""Gemini for the agent team: retries on 429/5xx and client-side pacing (Week 4, D-513).

Observed on the first live run: the Gemini free tier allows **5 requests per minute** for
gemini-2.5-flash. A five-agent incident needs 15-25 model calls, so the team hit HTTP 429
after about 30 seconds and the investigation failed.

Two fixes, used together:

* ``team_model()`` builds ADK's ``Gemini`` model with ``retry_options``: Google's client
  retries 429 and 5xx with exponential back-off (10, 20, 40 s, capped at 60 s) before ADK
  sees an error.
* ``pace_model_calls`` (an ADK ``before_model_callback`` on every team agent) waits *before*
  a request when the last minute already had ``COPILOT_LLM_RPM`` requests, so the quota is
  rarely hit at all. ``COPILOT_LLM_RPM=0`` (default) turns pacing off, for paid quotas.

The pacer is shared by all agents in the process, because the quota is per project and model.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque

from google.adk.models import BaseLlm, Gemini
from google.genai import types

from copilot.agents.diagnosis import DEFAULT_MODEL
from copilot.tools._http import int_from_env

__all__ = ["RequestPacer", "pace_model_calls", "pacer", "team_model"]

RETRY_OPTIONS = types.HttpRetryOptions(attempts=4, initial_delay=10.0, max_delay=60.0, exp_base=2.0, jitter=1.0,
                                       http_status_codes=[429, 500, 502, 503, 504])


def team_model(model: str | BaseLlm | None) -> BaseLlm:
    """A Gemini model with retries. A BaseLlm instance (e.g. a test's scripted model) is used as is."""
    if isinstance(model, BaseLlm):
        return model
    return Gemini(model=model or os.environ.get("COPILOT_MODEL", DEFAULT_MODEL), retry_options=RETRY_OPTIONS)


class RequestPacer:
    """At most ``rpm`` requests in any 60-second window (sliding window, shared by all agents)."""

    def __init__(self, rpm: int, window_seconds: float = 61.0):
        self.rpm = rpm
        self.window = window_seconds          # one second of margin over the server's minute
        self.calls: deque[float] = deque()
        self.waited_seconds = 0.0
        self._lock: asyncio.Lock | None = None
        self._loop = None

    def _loop_lock(self) -> asyncio.Lock:
        # A Lock belongs to one event loop; the detector starts a new loop for every alert.
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock, self._loop = asyncio.Lock(), loop
        return self._lock

    async def wait(self) -> float:
        if self.rpm <= 0:
            return 0.0
        async with self._loop_lock():
            waited = 0.0
            while True:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= self.window:
                    self.calls.popleft()
                if len(self.calls) < self.rpm:
                    self.calls.append(now)
                    self.waited_seconds += waited
                    return waited
                delay = self.window - (now - self.calls[0])
                await asyncio.sleep(delay)
                waited += delay


_PACER: RequestPacer | None = None


def pacer() -> RequestPacer:
    """The process-wide pacer, created on first use (after .env is loaded)."""
    global _PACER
    if _PACER is None:
        _PACER = RequestPacer(int_from_env("COPILOT_LLM_RPM", 0, 0, 100_000))
    return _PACER


async def pace_model_calls(callback_context, llm_request):
    """ADK before_model_callback: wait for a free slot, then let the request through."""
    await pacer().wait()
    return None
