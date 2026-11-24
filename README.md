# Incident Response Copilot

A multi-agent AI system that will **detect, investigate and help diagnose failures in a microservices
application** using only its telemetry: metrics, logs and traces. The test bed is the
[OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) ("Astronomy Shop"), release 3.1.0,
running locally in Docker. Faults are injected with the demo's feature flags, and the copilot must diagnose
them **without being able to read the flag state**.

## Status

| Phase | Scope | State | Docs |
|---|---|---|---|
| 0 | Environment, demo running, observability walkthrough | ✅ done | [phase-0-environment-setup.pdf](docs/phase-0-environment-setup.pdf) |
| 1 | Prometheus metrics tool (`query_metrics`) | ✅ done | [phase-1-prometheus-metrics-tool.pdf](docs/phase-1-prometheus-metrics-tool.pdf) |
| 1b | Observability tool layer: `search_logs`, `get_traces`, shared sanitization | ✅ done | [phase-1-observability-tools.pdf](docs/phase-1-observability-tools.pdf) |
| 2 | Single ADK diagnosis agent, health tools, threshold detector | ✅ built · Gemini runs pending | [phase-2-diagnosis-agent.pdf](docs/phase-2-diagnosis-agent.pdf) |

One Google ADK agent (Gemini) investigates an alert with the read-only tools and returns a structured diagnosis.
A detector watches error ratios and starts it automatically. Design decisions are recorded in
[DECISIONS.md](DECISIONS.md).

## Layout

```
copilot/agents/
  diagnosis.py    build_diagnosis_agent(), diagnose(alert) -> DiagnosisRun, Diagnosis schema
  console.py      transcript printing, runs/ saving
copilot/prompts/
  diagnosis.md    the agent's instructions (versioned like code)
detector/
  watcher.py      error ratio > 5 % for 2 min -> one alert -> diagnose()
copilot/tools/
  health.py       get_service_health(minutes), get_service_metrics(service, minutes)  golden signals, no PromQL
  metrics.py      query_metrics(promql, minutes, max_series=20)                     Prometheus
  logs.py         search_logs(service, level, text, minutes, limit, trace_id)       OpenSearch
  traces.py       get_traces(service, operation, errors_only, minutes, limit,
                             min_duration_ms, trace_id)                             Jaeger
  filters.py      feature-flag sanitization shared by all tools (no I/O)
  _http.py        the single read-only HTTP path (GET + allow-list, no proxy, size cap)
  __init__.py     TOOLS = (get_service_health, get_service_metrics, get_traces, search_logs, query_metrics)
tests/
  test_metrics.py, test_logs.py, test_traces.py, test_filters.py   unit tests (recorded responses)
  test_security.py                cross-tool invariants (no flag leaks, GET-only, never raise, ADK-ready)
  test_metrics_live.py, test_tools_live.py   integration tests (auto-skipped if backends are down)
  fixtures/{prometheus,opensearch,jaeger}/   real responses recorded from the demo (normal + fault state)
scripts/
  start_demo.sh                   make start + apply the copilot overlay
  record_tool_fixtures.py         re-record OpenSearch/Jaeger/Prometheus fixtures (normal | fault)
  record_prometheus_fixtures.py   re-record the original Prometheus fixtures
deploy/compose.copilot.yaml       overlay: OpenSearch and Jaeger on fixed loopback-only ports
docs/                             phase PDFs, their HTML sources, screenshots and example outputs
opentelemetry-demo/               upstream demo (tag 3.1.0) with two local changes (see DECISIONS.md)
```

## Quick start

```bash
# 1. Start the demo + overlay (≈4 GB of images on first run; ~10 GB RAM for Docker)
scripts/start_demo.sh             # or, if the demo is already running: scripts/start_demo.sh --overlay

# 2. Python environment (Python ≥ 3.11)
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# 3. Tests: unit tests are hermetic (network blocked, no Gemini); live tests run only if the backends answer
.venv/bin/python -m pytest            # everything (427 tests)
.venv/bin/python -m pytest -m live    # only the 13 live integration tests

# 4. The agent (needs a free Gemini key: cp .env.example .env, then fill GOOGLE_API_KEY)
.venv/bin/python scripts/run_diagnosis.py                 # one investigation, every step printed
.venv/bin/python scripts/run_diagnosis.py --runs 5        # run-to-run variation
.venv/bin/python -m detector.watcher --dry-run            # watch error ratios, print alerts only
.venv/bin/python -m detector.watcher                      # alert -> agent investigates automatically
```

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `PROMETHEUS_URL` | `http://localhost:9090` | fixed port published by the demo |
| `OPENSEARCH_URL` | `http://localhost:9200` | needs `deploy/compose.copilot.yaml` (upstream uses a random port) |
| `OPENSEARCH_LOGS_INDEX` | `otel-logs-*` | validated index pattern |
| `JAEGER_URL` | `http://localhost:16686/jaeger/ui` | direct to Jaeger (not via Envoy :8080, which would trace our own queries) |
| `PROMETHEUS_TIMEOUT_SECONDS` / `OPENSEARCH_TIMEOUT_SECONDS` / `JAEGER_TIMEOUT_SECONDS` | `10` | |
| `PROMETHEUS_MIN_STEP_SECONDS` | `60` | the demo's measured sample interval |
| `GOOGLE_API_KEY` | (none) | Gemini key from AI Studio, in `.env` (git-ignored) |
| `COPILOT_MODEL` | `gemini-2.5-flash` | any Gemini model the key can use |

## The tools

Every tool returns a dict with `status` = `ok` | `no_data` | `error` (with `error.type` and `error.message`)
and **never raises**.

### `query_metrics(promql, minutes, max_series=20)`
Range query → per-series `min/max/mean/first/latest/change/trend` (trend = last-quarter vs first-quarter
median, ±10 %). Example: `query_metrics('sum by (service_name) (rate(traces_span_metrics_calls_total[5m]))', 15)`.

### `search_logs(service=None, level=None, text=None, minutes=15, limit=20, trace_id=None)`
Returns `total_matches`, `by_service`, `by_level`, recurring message `patterns` (variable parts such as IDs and
numbers collapsed) and the most recent `records`. `level` is a minimum ("warn" = warn + error + fatal).
Time filtering uses `observedTimestamp`, because shipping/currency logs carry `@timestamp = 1970-01-01`.

### `get_traces(service=None, operation=None, errors_only=False, minutes=15, limit=10, min_duration_ms=None, trace_id=None)`
Per trace: `trace_id`, `duration_ms`, `services`, `failed_services`, `error_origins` (the deepest failing
spans) and up to 5 `relevant_spans` (error origins, other errors, then the largest *self time*). There is
also a `summary` with duration percentiles and counts by failed service and by origin. Traces still
arriving at query time are marked `incomplete`.

Real output (replayed from a recording made with `paymentFailure=50%` and `productCatalogFailure` active),
`get_traces(service="checkout", errors_only=True, minutes=5, limit=4)["summary"]`:

```json
{"trace_count": 4, "error_trace_count": 4, "incomplete_trace_count": 1,
 "duration_ms": {"min": 36.702, "p50": 46.741, "p95": 63.249, "max": 63.249, "mean": 50.58},
 "failed_services": {"checkout": 4, "frontend-proxy": 4, "load-generator": 4, "frontend": 3, "payment": 3},
 "error_origins": {"payment :: charge": 3, "frontend-proxy :: router frontend egress": 2,
                   "checkout :: oteldemo.ProductCatalogService/GetProduct": 1}}
```

The raw Jaeger response was 890 KiB; the tool output was 11 KiB, with no flag information. The two
non-payment origins come from the one trace marked `incomplete`.

## Security model

1. **Read-only by construction.** Every request goes through `_http.get_json`: always `GET`, path allow-listed
   per tool (`/api/v1/query_range`; `/<index>/_search`; `/api/traces`, `/api/traces/<hex id>`), redirects
   off, 10–32 MiB response cap. Tests check the AST: only `_http.py` imports httpx, and no write verbs appear
   anywhere.
2. **No proxy.** `trust_env=False`, so the shell's `HTTP(S)_PROXY`/`ALL_PROXY` (college proxy) are ignored.
3. **Feature-flag isolation** (`filters.py`), applied before anything is returned:
   - inputs that mention flags or the flag service are refused before any request (no counting oracle);
   - flagd/flagd-ui telemetry and calls to the flag service are dropped;
   - `feature_flag.*`, `demo.feature_flag.*` and `shipping.feature_flag.*` attributes and events are removed;
   - flag-revealing text is replaced as a whole by `[redacted]`;
   - log narration such as "Feature Flag adManualGc enabled…" is dropped;
   - OpenSearch exclusions run inside the query, so counts and aggregations are clean too;
   - `scrub_output` is the final safety net on every result.

   Verified against recordings made while four faults were active: 0 flag mentions in any output.
4. **No credentials in messages** (`user:pass@` is stripped). Unexpected payloads become `internal` errors.

## Network note (college proxy)

Docker Desktop is configured with a manual proxy (`172.31.2.3:8080`) because the network's PAC/WPAD setup breaks
image pulls. Switch it back to the system proxy on other networks. The Python tools deliberately ignore proxy
variables.
