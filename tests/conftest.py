"""Shared pytest fixtures: replay recorded backend responses, block real network.

Unit tests never touch the network: an autouse fixture replaces
``copilot.tools._http.make_client`` with a client whose transport fails the
test. Tests opt in to a recorded response with ``replay(...)`` or to a
transport failure with ``failing(...)``. Tests marked ``live`` or
``allow_network`` keep the real client.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from copilot.tools import _http

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """load_fixture("jaeger/trace_by_id") -> the recorded fixture dict."""
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


def fixture_names(folder: str) -> list[str]:
    return sorted(f"{folder}/{p.stem}" for p in (FIXTURE_DIR / folder).glob("*.json"))


class Recorder:
    """Transport handler serving one canned response and logging every request."""

    def __init__(self, status_code: int, body: Any, content_type: str = "application/json"):
        self.status_code = status_code
        self.body = body
        self.content_type = content_type
        self.requests: list[httpx.Request] = []
        self.base_urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        content = self.body if isinstance(self.body, (str, bytes)) else json.dumps(self.body)
        return httpx.Response(self.status_code, content=content, headers={"content-type": self.content_type})

    @property
    def last_json_body(self) -> Any:
        return json.loads(self.requests[-1].content) if self.requests and self.requests[-1].content else None


def _blocked(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected network call in a unit test: {request.method} {request.url}")


CONFIG_VARS = (
    "PROMETHEUS_URL", "PROMETHEUS_TIMEOUT_SECONDS", "PROMETHEUS_MIN_STEP_SECONDS",
    "OPENSEARCH_URL", "OPENSEARCH_LOGS_INDEX", "OPENSEARCH_TIMEOUT_SECONDS",
    "JAEGER_URL", "JAEGER_TIMEOUT_SECONDS",
)


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return
    for var in CONFIG_VARS:  # unit tests must not depend on the developer's environment
        monkeypatch.delenv(var, raising=False)
    if request.node.get_closest_marker("allow_network"):
        return
    monkeypatch.setattr(_http, "make_client",
                        lambda base_url, timeout: httpx.Client(base_url=base_url, transport=httpx.MockTransport(_blocked)))


@pytest.fixture
def replay(monkeypatch) -> Callable[..., Recorder]:
    """replay("jaeger/trace_by_id") or replay(status_code=..., body=...) -> Recorder."""

    def make(name: str | None = None, *, status_code: int = 200, body: Any = None,
             content_type: str = "application/json") -> Recorder:
        if name is not None:
            resp = load_fixture(name)["response"]
            status_code, body, content_type = resp["status_code"], resp["body"], resp["content_type"]
        recorder = Recorder(status_code, body, content_type)

        def factory(base_url: str, timeout: float) -> httpx.Client:
            recorder.base_urls.append(base_url)
            return httpx.Client(base_url=base_url, transport=httpx.MockTransport(recorder))

        monkeypatch.setattr(_http, "make_client", factory)
        return recorder

    return make


@pytest.fixture
def failing(monkeypatch) -> Callable[[Exception], list]:
    """failing(exc) -> list of attempted requests; every request raises ``exc``."""

    def make(exc: Exception) -> list:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            raise exc

        monkeypatch.setattr(_http, "make_client",
                            lambda base_url, timeout: httpx.Client(base_url=base_url,
                                                                    transport=httpx.MockTransport(handler)))
        return seen

    return make
