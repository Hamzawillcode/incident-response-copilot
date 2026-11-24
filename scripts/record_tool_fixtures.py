"""Record real OpenSearch / Jaeger / Prometheus responses as pytest fixtures.

Usage (with the demo and deploy/compose.copilot.yaml applied):
    .venv/bin/python scripts/record_tool_fixtures.py normal
    .venv/bin/python scripts/record_tool_fixtures.py fault    # while faults are injected

Two kinds of recordings:
* tool recordings - the real tool runs; a recording transport stores the exact
  request it sent and the raw response it got (so replay tests exercise the
  real request builders);
* raw recordings  - hand-built GET requests that bypass the tools' guards and
  server-side exclusions, to capture leaking data and error payloads that the
  client-side defences must handle.

Read-only: every request is a GET (OpenSearch _search with a JSON body; Jaeger
query API). Fixtures are test data and are never shown to an agent.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from copilot.tools import _http, get_traces, query_metrics, search_logs  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
OPENSEARCH = "http://localhost:9200"
JAEGER = "http://localhost:16686/jaeger/ui"


class RecordingTransport(httpx.BaseTransport):
    def __init__(self):
        self.inner = httpx.HTTPTransport()
        self.exchanges: list[tuple[httpx.Request, httpx.Response, bytes]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        resp = self.inner.handle_request(request)
        content = resp.read()
        self.exchanges.append((request, resp, content))
        # `content` is already decoded: drop encoding/length headers so it is not decoded twice.
        headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in ("content-encoding", "content-length")]
        return httpx.Response(resp.status_code, headers=headers, content=content)


def _save(folder: str, name: str, description: str, request: httpx.Request, status: int, content_type: str,
          content: bytes) -> None:
    try:
        body = json.loads(content)
    except ValueError:
        body = content.decode("utf-8", errors="replace")
    req_body = json.loads(request.content) if request.content else None
    fixture = {
        "description": description,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": {"method": request.method, "path": request.url.path,
                    "params": dict(request.url.params), "json_body": req_body},
        "response": {"status_code": status, "content_type": content_type, "body": body},
    }
    out = FIXTURES / folder / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=1) + "\n")
    print(f"  {folder}/{name:34s} HTTP {status}  {len(content)/1024:8.1f} KiB")


def record_tool(folder: str, name: str, description: str, call) -> dict:
    transport = RecordingTransport()
    original = _http.make_client
    _http.make_client = lambda base_url, timeout: httpx.Client(
        base_url=base_url, timeout=timeout, transport=transport, trust_env=False)
    try:
        result = call()
    finally:
        _http.make_client = original
    (req, resp, content), = transport.exchanges
    _save(folder, name, description, req, resp.status_code, resp.headers.get("content-type", ""), content)
    return result


def record_raw(folder: str, name: str, description: str, base: str, path: str, params=None, json_body=None):
    with httpx.Client(base_url=base, timeout=20, trust_env=False) as c:
        content = json.dumps(json_body).encode() if json_body is not None else None
        headers = {"Content-Type": "application/json"} if content else {}
        req = c.build_request("GET", path, params=params, content=content, headers=headers)
        resp = c.send(req)
        _save(folder, name, description, req, resp.status_code, resp.headers.get("content-type", ""), resp.content)


def _recent_window(minutes: int):
    end = time.time()
    return end - minutes * 60, end


def normal() -> None:
    print("normal-state recordings")
    record_tool("opensearch", "logs_recent", "search_logs(minutes=15, limit=20): all services.",
                lambda: search_logs(minutes=15, limit=20))
    record_tool("opensearch", "logs_checkout", 'search_logs(service="checkout", minutes=30).',
                lambda: search_logs(service="checkout", minutes=30))
    record_tool("opensearch", "logs_warn", 'search_logs(level="warn", minutes=120).',
                lambda: search_logs(level="warn", minutes=120))
    record_tool("opensearch", "logs_text", 'search_logs(text="Product Found", minutes=15, limit=5).',
                lambda: search_logs(text="Product Found", minutes=15, limit=5))
    record_tool("opensearch", "logs_empty", 'search_logs(service="does-not-exist", minutes=5).',
                lambda: search_logs(service="does-not-exist", minutes=5))
    record_raw("opensearch", "raw_flag_records",
               "RAW: flag-related records the server-side exclusion normally removes (Envoy /flagservice "
               "access logs, shipping.feature_flag.evaluated events).",
               OPENSEARCH, "/otel-logs-*/_search", json_body={
                   "size": 30, "track_total_hits": True, "sort": [{"observedTimestamp": {"order": "desc"}}],
                   "query": {"query_string": {"query": "*flagservice* OR *feature_flag* OR *flagd*",
                                              "fields": ["body", "eventName"], "analyze_wildcard": True}}})
    record_raw("opensearch", "raw_bad_request", "RAW: invalid date in a range query -> HTTP 400.",
               OPENSEARCH, "/otel-logs-*/_search",
               json_body={"query": {"range": {"observedTimestamp": {"gte": "not-a-date"}}}})

    record_tool("jaeger", "traces_checkout", 'get_traces(service="checkout", minutes=15, limit=3).',
                lambda: get_traces(service="checkout", minutes=15, limit=3))
    r = record_tool("jaeger", "traces_fraud_errors",
                    'get_traces(service="fraud-detection", errors_only=True, minutes=60): only flagd errors.',
                    lambda: get_traces(service="fraud-detection", errors_only=True, minutes=60, limit=3))
    record_tool("jaeger", "traces_loadgen_errors",
                'get_traces(service="load-generator", errors_only=True, minutes=15, limit=2): /prompt noise.',
                lambda: get_traces(service="load-generator", errors_only=True, minutes=15, limit=2))
    tid = get_traces(service="checkout", operation="oteldemo.CheckoutService/PlaceOrder", minutes=15, limit=1)
    trace_id = tid["traces"][0]["trace_id"]
    record_tool("jaeger", "trace_by_id", f"get_traces(trace_id={trace_id!r}): one PlaceOrder trace.",
                lambda: get_traces(trace_id=trace_id))
    record_tool("jaeger", "trace_not_found", 'get_traces(trace_id="0"*32): 404.',
                lambda: get_traces(trace_id="0" * 32))
    record_tool("jaeger", "traces_unknown_service", 'get_traces(service="does-not-exist").',
                lambda: get_traces(service="does-not-exist", minutes=5))
    start, end = _recent_window(15)
    record_raw("jaeger", "raw_missing_service", "RAW: /api/traces without service -> HTTP 400.",
               JAEGER, "/api/traces", params={"start": str(int(start * 1e6)), "end": str(int(end * 1e6)), "limit": "1"})
    del r


def fault() -> None:
    print("fault-state recordings (productCatalogFailure=on, paymentFailure=50%, adManualGc=on, "
          "intlShippingSlowdown=5sec)")
    record_tool("jaeger", "fault_product_catalog_errors",
                'FAULT: get_traces(service="product-catalog", errors_only=True, minutes=5, limit=3).',
                lambda: get_traces(service="product-catalog", errors_only=True, minutes=5, limit=3))
    record_tool("jaeger", "fault_checkout_errors",
                'FAULT: get_traces(service="checkout", errors_only=True, minutes=5, limit=4).',
                lambda: get_traces(service="checkout", errors_only=True, minutes=5, limit=4))
    record_tool("opensearch", "fault_logs_warn",
                'FAULT: search_logs(level="warn", minutes=5).', lambda: search_logs(level="warn", minutes=5))
    record_raw("opensearch", "fault_raw_narration",
               "RAW FAULT: unexcluded warn+ records and fault narration (e.g. 'Feature Flag adManualGc enabled').",
               OPENSEARCH, "/otel-logs-*/_search", json_body={
                   "size": 40, "track_total_hits": True, "sort": [{"observedTimestamp": {"order": "desc"}}],
                   "query": {"bool": {"should": [
                       {"range": {"severity.number": {"gte": 13}}},
                       {"query_string": {"query": "*eature*lag* OR *intlShippingSlowdown* OR *adManualGc*",
                                         "fields": ["body"], "analyze_wildcard": True}}],
                       "minimum_should_match": 1,
                       "filter": [{"range": {"observedTimestamp": {"gte": "now-5m"}}}]}}})
    record_tool("prometheus", "fault_error_ratio",
                "FAULT: error ratio per service over 15 minutes.",
                lambda: query_metrics(
                    'sum by (service_name) (rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}[2m]))'
                    " / sum by (service_name) (rate(traces_span_metrics_calls_total[2m]))", 15))


if __name__ == "__main__":
    {"normal": normal, "fault": fault}[sys.argv[1] if len(sys.argv) > 1 else "normal"]()
