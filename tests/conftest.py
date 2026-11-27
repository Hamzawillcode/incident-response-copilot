"""Shared pytest fixtures: replay recorded backend responses, block real network.

Unit tests never touch the network: an autouse fixture replaces
``copilot.tools._http.make_client`` with a client whose transport fails the
test, blocks knowledge-base database connections and makes sure no test can
call the Gemini embedding API. Tests opt in to a recorded response with
``replay(...)``, to a transport failure with ``failing(...)``, to an offline
knowledge base with ``kb_offline`` and to a private schema in the real
knowledge-base Postgres with ``kb_db`` (marker ``db``). Tests marked ``live`` or
``allow_network`` keep the real HTTP client.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
import psycopg
import pytest

from copilot.kb import embeddings as kb_embeddings
from copilot.kb import store as kb_store
from copilot.kb.store import StoreError
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
    "KB_DATABASE_URL", "KB_SCHEMA", "KB_EMBEDDING_MODEL", "KB_EMBEDDING_TIMEOUT_SECONDS",
    "KB_CONNECT_TIMEOUT_SECONDS", "KB_STATEMENT_TIMEOUT_MS",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
    "FLAGD_CONFIG_PATH", "COPILOT_ACTIONS_DIR", "COPILOT_ACTIONS_DRY_RUN", "COPILOT_SESSIONS",
    "COPILOT_ADK_SCHEMA", "COPILOT_POSTMORTEM_DIR", "COPILOT_OTLP_ENDPOINT",
)
# Read before any test clears it: db tests use the developer's knowledge-base URL, if set.
_KB_DATABASE_URL = os.environ.get("KB_DATABASE_URL")


def _blocked_db(*args, **kwargs):
    raise AssertionError("unexpected knowledge-base database connection in a unit test")


def _blocked_subprocess(*args, **kwargs):
    raise AssertionError(f"unexpected subprocess call in a test: {args[:1]}")


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch, tmp_path):
    # Remediation can never touch the real demo or the real runs/ folder from a test (Week 4):
    # the settings file points nowhere, action logs and postmortem drafts go to tmp_path,
    # and `docker restart` is blocked unless a test replaces subprocess.run itself.
    from copilot.actions import remediation
    monkeypatch.setattr(remediation, "run_command", _blocked_subprocess)
    if request.node.get_closest_marker("live"):
        return
    for var in CONFIG_VARS:  # unit tests must not depend on the developer's environment
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FLAGD_CONFIG_PATH", str(tmp_path / "no-settings-file.json"))
    monkeypatch.setenv("COPILOT_ACTIONS_DIR", str(tmp_path / "actions"))
    monkeypatch.setenv("COPILOT_POSTMORTEM_DIR", str(tmp_path / "postmortems"))
    # No test may call the Gemini embedding API; tests that need vectors inject a fake embedder.
    monkeypatch.setattr(kb_embeddings, "make_embedder", lambda: None)
    if not request.node.get_closest_marker("db"):
        monkeypatch.setattr(kb_store, "connect", _blocked_db)
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


@pytest.fixture
def kb_offline(monkeypatch) -> list[bool]:
    """The knowledge base is unreachable: every connection attempt raises StoreError("connection")."""
    attempts: list[bool] = []

    def connect(*, read_only: bool = True):
        attempts.append(read_only)
        raise StoreError("connection", "Cannot reach the knowledge base (unit test). "
                                       "Start it with `docker compose up -d kb-postgres`.")

    monkeypatch.setattr(kb_store, "connect", connect)
    return attempts


@pytest.fixture
def kb_db(request, monkeypatch):
    """A private, empty schema in the real knowledge-base Postgres (dropped afterwards).

    Tests using it must be marked ``db``; they are skipped when the database is not running.
    """
    assert request.node.get_closest_marker("db"), "tests using kb_db must be marked @pytest.mark.db"
    if _KB_DATABASE_URL:
        monkeypatch.setenv("KB_DATABASE_URL", _KB_DATABASE_URL)
    try:
        admin = psycopg.connect(kb_store.database_url(), connect_timeout=2, autocommit=True)
    except psycopg.Error:
        pytest.skip("knowledge-base Postgres is not running (docker compose up -d kb-postgres)")
    schema = f"kb_test_{uuid.uuid4().hex[:10]}"
    with admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
        admin.execute(f"CREATE SCHEMA {schema}")
        monkeypatch.setenv("KB_SCHEMA", schema)
        monkeypatch.setenv("COPILOT_ADK_SCHEMA", f"{schema}_adk")   # ADK session tables (Week 4)
        try:
            yield schema
        finally:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")
            admin.execute(f"DROP SCHEMA IF EXISTS {schema}_adk CASCADE")
