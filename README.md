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
| 3 | Knowledge base: 14 runbooks + 5 postmortems in pgvector, hybrid search tools (RAG) | ✅ built · retrieval measured · agent runs pending | [phase-3-knowledge-base-rag.pdf](docs/phase-3-knowledge-base-rag.pdf) |
| 4 | Agent team (triage, knowledge, diagnosis, reviewer, action), human approval, Postgres state, tracing | ✅ built · 678 tests pass · 9 live runs (fix approved & resolved, rejected, false alarm) | [phase-4-agent-team.pdf](docs/phase-4-agent-team.pdf) |

A team of five Google ADK agents (Gemini) investigates an alert: **triage** takes a first look, **knowledge**
searches runbooks and past incidents (RAG over Postgres + pgvector), **diagnosis** finds the root cause with
evidence, a **reviewer** checks that evidence against the real tool results, and **action** proposes one fix. The
fix only runs after a human approves it. A detector watches error ratios and starts the team automatically. Design
decisions are recorded in [DECISIONS.md](DECISIONS.md).

```
alert -> triage -> knowledge -> diagnosis <-> reviewer (max 2 rounds) -> action -> HUMAN approves -> fix -> verify
                                                                               \-> nothing changes until then
```

## Layout

```
copilot/agents/
  coordinator.py  IncidentCoordinator: investigate(alert), decide(id, approve) -- the fixed procedure, in code
  team.py         the five agents and their output schemas (TriageReport, KnowledgeReport, Review)
  runtime.py      run_step(): one agent step -> transcript, tokens, parsed output, paused approval; tool_guard
  review.py       grounding check: cited trace IDs / services must exist in real tool results
  verify.py       after a fix: error ratios back below 5 %?
  diagnosis.py    build_diagnosis_agent(knowledge=False, team=False), diagnose(alert), Diagnosis schema
  console.py      transcript printing, runs/ saving
copilot/actions/          the ONLY code that changes anything (Action agent only, human approval required)
  remediation.py  rollback_config, restart_service, escalate_to_human; audit log, backups, dry run
copilot/state.py          incident records (Postgres table incidents) + ADK sessions (Postgres schema adk)
copilot/postmortem.py     draft postmortem from an incident record (runs/postmortems/, not ingested)
copilot/telemetry.py      OpenTelemetry tracing of the copilot itself (to copilot-jaeger)
copilot/prompts/
  diagnosis.md              the agent's instructions (versioned like code)
  diagnosis_knowledge.md    extra section when the knowledge tools are attached
  diagnosis_team.md         extra section in the team: triage report, knowledge, reviewer feedback
  triage.md, knowledge.md, reviewer.md, action.md
copilot/kb/                 knowledge base (Week 3)
  documents.py    read kb/*.md, validate, one chunk per "##" section (refuses fault-injection terms)
  embeddings.py   Gemini embeddings: RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY, 768 dims, retries, query cache
  store.py        Postgres connection (read-only for searches), keyword and vector SELECTs
  search.py       hybrid retrieval: keyword + vector, Reciprocal Rank Fusion, grouped by document
  ingest.py       python -m copilot.kb.ingest  (incremental: only changed sections are embedded)
kb/
  runbooks/       14 runbooks, one per service area + triage + known noise
  postmortems/    5 seed postmortems (made up, see kb/README.md)
detector/
  watcher.py      error ratio > 5 % for 2 min -> one alert -> diagnose()
copilot/tools/
  health.py       get_service_health(minutes), get_service_metrics(service, minutes)  golden signals, no PromQL
  metrics.py      query_metrics(promql, minutes, max_series=20)                     Prometheus
  logs.py         search_logs(service, level, text, minutes, limit, trace_id)       OpenSearch
  traces.py       get_traces(service, operation, errors_only, minutes, limit,
                             min_duration_ms, trace_id)                             Jaeger
  kb.py           search_runbooks(query, k=4), search_postmortems(query, k=3)             knowledge base (RAG)
  filters.py      feature-flag sanitization shared by all tools (no I/O)
  _http.py        the single read-only HTTP path (GET + allow-list, no proxy, size cap)
  __init__.py     TOOLS = (get_service_health, get_service_metrics, get_traces, search_logs, query_metrics)
                  KB_TOOLS = (search_runbooks, search_postmortems)
tests/
  test_metrics.py, test_logs.py, test_traces.py, test_filters.py   unit tests (recorded responses)
  test_security.py                cross-tool invariants (no flag leaks, GET-only, never raise, ADK-ready)
  test_kb_*.py                    knowledge base: documents, embeddings, search, tools (unit) and test_kb_db.py (db)
  test_metrics_live.py, test_tools_live.py   integration tests (auto-skipped if backends are down)
  fixtures/{prometheus,opensearch,jaeger}/   real responses recorded from the demo (normal + fault state)
scripts/
  start_demo.sh                   make start + apply the copilot overlay
  record_tool_fixtures.py         re-record OpenSearch/Jaeger/Prometheus fixtures (normal | fault)
  record_prometheus_fixtures.py   re-record the original Prometheus fixtures
  check_retrieval.py              hit@1 / hit@3 / MRR on eval/retrieval_queries.yaml
  incident.py                     run | approve | reject | list | show  (the agent team)
eval/retrieval_queries.yaml       28 test queries with their expected documents
docker-compose.yml                kb-postgres (Postgres 17 + pgvector, 127.0.0.1:5433), copilot-jaeger (UI 16687)
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

# 3. Knowledge base (Week 3): Postgres + pgvector, then load kb/ into it
docker compose up -d kb-postgres
.venv/bin/python -m copilot.kb.ingest                  # without GOOGLE_API_KEY: text only, keyword search
.venv/bin/python scripts/check_retrieval.py            # hit@1 / hit@3 / MRR on 28 test queries (--mode keyword|vector|hybrid)

# 4. Tests: unit tests are hermetic (no network, no database, no Gemini); db and live tests skip if their backend is down
.venv/bin/python -m pytest            # everything
.venv/bin/python -m pytest -m db      # only the database tests (knowledge base, incident state)
.venv/bin/python -m pytest -m live    # only the live integration tests (demo running)

# 5. The agent (needs a free Gemini key: cp .env.example .env, then fill GOOGLE_API_KEY)
.venv/bin/python scripts/run_diagnosis.py                 # one investigation, every step printed
.venv/bin/python scripts/run_diagnosis.py --runs 5        # run-to-run variation
.venv/bin/python scripts/run_diagnosis.py --knowledge     # + runbook / postmortem search (RAG)
.venv/bin/python -m copilot.kb.ingest                     # after adding the key: embeds all 116 sections once

# 6. The agent team (Week 4): investigates, then STOPS until a human decides
#    Free tier: set COPILOT_LLM_RPM=5 (5 requests/minute). The free tier also allows only 20 requests a day per
#    model, and one incident takes about 8, so use a second model (COPILOT_MODEL=gemini-3.5-flash) when it runs out.
docker compose up -d copilot-jaeger                       # optional: traces of the copilot (UI :16687)
.venv/bin/python scripts/incident.py run                  # -> status awaiting_approval + the approve command
.venv/bin/python scripts/incident.py approve <id> --verify-minutes 8    # or: reject <id> --note "why"
.venv/bin/python scripts/incident.py list                 # incidents stored in Postgres
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
| `KB_DATABASE_URL` | `postgresql://copilot:copilot@localhost:5433/copilot` | the `kb-postgres` container |
| `KB_SCHEMA` | `public` | Postgres schema of the `kb_chunks` table (tests use a private one) |
| `KB_EMBEDDING_MODEL` | `gemini-embedding-001` | 768-dim output; rows remember the model that embedded them |
| `KB_EMBEDDING_TIMEOUT_SECONDS` / `KB_STATEMENT_TIMEOUT_MS` | `10` / `5000` | query embedding, SQL statements |
| `COPILOT_LLM_RPM` | `0` (off) | pace the team to N model requests a minute; `5` for the Gemini free tier |
| `COPILOT_SESSIONS` | `postgres` | ADK sessions in Postgres (schema `COPILOT_ADK_SCHEMA`, default `adk`) or `memory` |
| `COPILOT_OTLP_ENDPOINT` | (none) | e.g. `http://localhost:4328/v1/traces` (copilot-jaeger); never the demo's collector |
| `COPILOT_ACTIONS_DRY_RUN` | (off) | `1`: actions report what they would change, without changing it |
| `FLAGD_CONFIG_PATH` | `opentelemetry-demo/src/flagd/demo.flagd.json` | the settings file `rollback_config` restores |

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

### `search_runbooks(query, k=4)` and `search_postmortems(query, k=3)`
Search the knowledge base in `kb/` (Week 3). The query is plain words describing the symptoms. The result has
`retrieval` (`hybrid` or `keyword`) and up to `k` documents, each with `source`, `title`, `services`, `score`,
`matched_by` and its two best `sections`. Postmortems also have `incident_id`, `date`, `root_cause_service` and
`failure_type`. Every result carries a `note` saying it is background knowledge, not evidence.

Retrieval combines Postgres full-text search (exact words, title and headings weigh most) and pgvector cosine
similarity on Gemini embeddings (paraphrases), fused with Reciprocal Rank Fusion. Without a key, or when the
embedding API fails, it falls back to keyword search and says so.

Measured on 28 labelled test queries (`scripts/check_retrieval.py`, 2026-11-27, details in DECISIONS.md D-411):

| Mode | hit@3 (all 28) | Paraphrases (8): hit@1 / hit@3 |
|---|---|---|
| keyword only | 0.93 | 0.50 / 0.75 |
| vector only | 1.00 | 0.88 / 1.00 |
| hybrid (default) | 1.00 | 0.75 / 1.00 |

"checkout errors" returns the checkout runbook first in every mode (the Week-3 target was top 3). The queries were
written by the author of the documents, so treat this as a regression check, not a benchmark.

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
5. **Knowledge base:** documents that mention fault injection are refused at ingestion (and a test scans every
   file); searches use a read-only Postgres session with a statement timeout; only `copilot/kb/ingest.py` contains
   write SQL; all values are query parameters.
6. **Actions (Week 4):** only the Action agent has write tools, and only in `copilot/actions/` (the read-only tools
   never import it). Every write call needs a human: ADK tool confirmation pauses the run, and the tool itself
   refuses to run without a confirmed approval. One action per incident, approvals apply exactly once, every call is
   audited, restarts are limited to the shop's own services, and results never name the fault-injection settings.

## Network note (college proxy)

Docker Desktop is configured with a manual proxy (`172.31.2.3:8080`) because the network's PAC/WPAD setup breaks
image pulls. Switch it back to the system proxy on other networks. The Python tools deliberately ignore proxy
variables.
