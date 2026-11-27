"""Cross-tool security and contract invariants.

These tests state the guarantees the future agents rely on, across all tools:
no feature-flag information in any output, read-only GET traffic, no raising,
strict-JSON results, and signatures that an agent framework can register.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import typing
from pathlib import Path

import httpx
import pytest

from copilot.tools import (
    KB_TOOLS, TOOLS, filters, get_traces, query_metrics, search_logs, search_postmortems, search_runbooks,
)
from tests.conftest import fixture_names, load_fixture

LEAK = re.compile(r"flag|ofrep|openfeature|" + "|".join(filters.FLAG_KEYS), re.IGNORECASE)
TOOLS_DIR = Path(__file__).resolve().parent.parent / "copilot" / "tools"
KB_QUERY_MODULES = [Path(__file__).resolve().parent.parent / "copilot" / "kb" / name
                    for name in ("store.py", "search.py", "embeddings.py")] + [TOOLS_DIR / "kb.py"]
ALL_FIXTURES = fixture_names("prometheus") + fixture_names("opensearch") + fixture_names("jaeger")


def _call_tool_for(fixture: str) -> dict:
    folder, name = fixture.split("/")
    if folder == "prometheus":
        return query_metrics("recorded_metric_total", 30, max_series=200)
    if folder == "opensearch":
        return search_logs(minutes=60, limit=100)
    if name in ("trace_by_id", "trace_not_found"):
        return get_traces(trace_id="a" * 32)
    return get_traces(service="checkout", minutes=60, limit=50)


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_no_flag_information_in_any_tool_output(replay, fixture):
    replay(fixture)
    result = _call_tool_for(fixture)
    text = json.dumps(result, allow_nan=False)  # also: strict JSON
    assert not LEAK.search(text), f"{fixture}: {LEAK.search(text).group(0)!r} leaked"
    assert result["status"] in ("ok", "no_data", "error")


def test_fixture_corpus_really_contains_flag_data():
    """Guard against the leak test passing vacuously."""
    leaking = [f for f in ALL_FIXTURES if LEAK.search(json.dumps(load_fixture(f)["response"]["body"]))]
    assert len(leaking) >= 8, leaking


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_every_request_is_a_single_get(replay, fixture):
    rec = replay(fixture)
    _call_tool_for(fixture)
    assert len(rec.requests) == 1
    assert rec.requests[0].method == "GET"


def test_only_the_http_module_imports_httpx():
    for path in TOOLS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imports |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        if path.name != "_http.py":
            assert "httpx" not in imports, f"{path.name} must use copilot.tools._http, not httpx directly"


def test_no_write_http_methods_anywhere_in_the_tools():
    source = "\n".join(p.read_text() for p in TOOLS_DIR.glob("*.py"))
    assert not re.search(r"""["'](POST|PUT|PATCH|DELETE)["']|\.(post|put|patch|delete)\(""", source)


# --------------------------------------------------------------------------- #
# Contract: never raise, always a dict with a status
# --------------------------------------------------------------------------- #
WEIRD = [None, "", " ", 0, -1, 10**12, 3.5, True, [], {}, object(), "\x00", "ä" * 5000, "{", "}"]


@pytest.mark.parametrize("value", WEIRD, ids=repr)
def test_tools_never_raise_on_hostile_input(failing, value):
    failing(httpx.ConnectError("no backend in unit tests"))  # inputs that are valid reach the transport
    for result in (
        query_metrics(value, value),
        query_metrics("up", value),
        search_logs(service=value, level=value, text=value, minutes=value, limit=value, trace_id=value),
        search_logs(text=value),
        get_traces(service=value, operation=value, errors_only=value, minutes=value, limit=value,
                   min_duration_ms=value, trace_id=value),
        get_traces(service="checkout", operation=value),
        search_runbooks(value, value),
        search_runbooks("checkout errors", value),
        search_postmortems(value),
    ):
        assert isinstance(result, dict) and result["status"] in ("ok", "no_data", "error")
        json.dumps(result, default=str)


# --------------------------------------------------------------------------- #
# Agent-framework readiness (e.g. Google ADK builds tool schemas from signatures)
# --------------------------------------------------------------------------- #
SIMPLE = {str, int, bool, float, type(None)}


@pytest.mark.parametrize("tool", TOOLS + KB_TOOLS, ids=lambda t: t.__name__)
def test_tool_signatures_use_only_json_friendly_parameters(tool):
    hints = typing.get_type_hints(tool)
    assert hints.pop("return") == dict[str, typing.Any]
    for name, hint in hints.items():
        args = typing.get_args(hint) or (hint,)
        assert set(args) <= SIMPLE, f"{tool.__name__}.{name}: {hint}"
    doc = inspect.getdoc(tool)
    assert "Args:" in doc and "Returns:" in doc
    for name in inspect.signature(tool).parameters:
        assert f"{name}:" in doc, f"{tool.__name__} docstring does not document {name}"


# --------------------------------------------------------------------------- #
# Findings from the final security/correctness review
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("call, body", [
    (lambda: get_traces(service="checkout"),
     {"data": [{"traceID": "a" * 32, "processes": {}, "spans": [{"spanID": "1", "startTime": "abc"}]}]}),
    (lambda: search_logs(), {"hits": {"total": {"value": 1}, "hits": [{"_source": {"severity": "oops"}}]}}),
    (lambda: query_metrics("up", 5), {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": 1}]}}),
])
def test_malformed_payloads_become_internal_errors_not_exceptions(replay, call, body):
    replay(body=body)
    result = call()
    assert result["status"] == "error" and result["error"]["type"] == "internal"


def test_undecodable_body_is_an_http_error(monkeypatch):
    """A body that claims gzip but is not: httpx raises DecodingError, which must not escape."""
    from copilot.tools import _http

    def handler(request):
        return httpx.Response(200, content=b"\x00not-gzip", headers={"content-encoding": "gzip"})

    monkeypatch.setattr(_http, "make_client",
                        lambda base_url, timeout: httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler)))
    assert get_traces(service="checkout")["error"]["type"] == "http_error"


def test_credentials_never_appear_in_messages(failing, monkeypatch):
    failing(httpx.ConnectError("refused"))
    monkeypatch.setenv("OPENSEARCH_URL", "http://admin:s3cret@search.internal:9200")
    message = search_logs()["error"]["message"]
    assert "s3cret" not in message and "admin" not in message and "***@search.internal:9200" in message
    monkeypatch.setenv("JAEGER_URL", "ftp://admin:s3cret@j")
    assert "s3cret" not in get_traces(service="checkout")["error"]["message"]


def test_safe_url():
    from copilot.tools._http import safe_url
    assert safe_url("http://u:p@h:1/x") == "http://***@h:1/x"
    assert safe_url("http://h:1/x") == "http://h:1/x"


# --------------------------------------------------------------------------- #
# Knowledge base (Week 3): the search path cannot write
# --------------------------------------------------------------------------- #
_SQL_WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM|DROP\s|CREATE\s|ALTER\s|TRUNCATE\b|GRANT\b)")


@pytest.mark.parametrize("path", KB_QUERY_MODULES, ids=lambda p: p.name)
def test_query_time_knowledge_base_code_contains_no_write_sql(path):
    """Writes live only in copilot/kb/ingest.py; searches run in a read-only session as well."""
    match = _SQL_WRITE.search(path.read_text())
    assert match is None, f"{path.name}: {match.group(0)!r}"


def test_knowledge_tools_open_read_only_sessions(monkeypatch):
    from tests.kb_fakes import FakeKb, candidate

    fake = FakeKb(keyword=[candidate("kb/runbooks/payment.md")]).install(monkeypatch)
    search_runbooks("payment errors")
    search_postmortems("payment errors")
    assert fake.connections == [True, True]


# --------------------------------------------------------------------------- #
# Week 4: the write path is isolated
# --------------------------------------------------------------------------- #
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "copilot" / "prompts"


def test_read_only_tools_never_import_the_remediation_package():
    for path in TOOLS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not any(m.startswith("copilot.actions") for m in modules), path.name


def test_no_prompt_mentions_the_fault_injection_mechanism():
    prompts = sorted(PROMPTS_DIR.glob("*.md"))
    assert {p.stem for p in prompts} >= {"diagnosis", "triage", "knowledge", "reviewer", "action"}
    for path in prompts:
        match = LEAK.search(path.read_text())
        assert match is None, f"{path.name}: {match.group(0)!r}"
