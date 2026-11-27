# Decisions

Architecture and engineering decisions, newest phase last. Each entry gives the context, the decision, and its
consequences. "Evidence" points to what we actually observed.

---

## Phase 0: Environment

### D-001 Pin the OpenTelemetry Demo to release 3.1.0 (code and images)
- **Context:** `.env` ships with `DEMO_VERSION=latest`, so image tags float independently of the cloned code.
- **Decision:** Shallow-clone tag `3.1.0` and set `DEMO_VERSION=3.1.0` in `opentelemetry-demo/.env.override`.
- **Consequences:** Configs on disk and running images match; docs and fixtures are reproducible. Upgrading
  is a deliberate change.

### D-002 Run core + full + observability + extras; skip `compose.agent.yaml`
- **Context:** The agent layer adds the demo's own LLM chatbot (needs an API key). We are building our own agents.
- **Decision:** Use `make start` (28 services).
- **Consequences:** The load generator's hard-coded `/prompt` task fails with DNS errors (~6.5 % baseline
  failures). This is known noise, not an incident.

### D-003 Docker Desktop uses the college proxy explicitly
- **Context:** Every image pull failed with `TLS handshake timeout`. Evidence: Docker's `httpproxy.log` showed
  `evaluating PAC file … lookup wpad: no such host`, then stalled direct connections. `http://wpad/wpad.dat`
  returns an HTML filter page instead of a PAC file.
- **Decision:** Docker Desktop → Proxies → manual `http://172.31.2.3:8080` (backup of the settings file kept).
- **Consequences:** Pulls work on campus. On other networks, switch back to the system proxy.

### D-004 Fix OpenSearch log loss in the Collector, not in the apps
- **Context:** `cart`'s .NET log template emits attribute `http="8080"`. When it is the first document of a
  day, OpenSearch maps `attributes.http` as text and then rejects every log carrying `http.request.method`.
- **Decision:** Add `transform/avoid_mapping_conflicts` to `otelcol-config-extras.yml` (upstream's documented
  customisation seam) to rename root-level *string* attributes named after semantic-convention namespaces
  to `<name>_value`.
- **Consequences:** 0 exporter errors after the fix. Because arrays replace on merge, the extras file repeats
  the upstream logs `processors` list, which must be kept in sync on upgrades.

### D-005 Diagnosis agents must never see feature-flag state
- **Context:** Faults are injected with flagd feature flags. Evidence from a live trace: span events
  `feature_flag.evaluation` carry flag key and variant; flagd's own spans carry them as attributes.
- **Decision:** Agents get telemetry only through tools that strip flag information. Fault injection lives
  outside the agents' reach.
- **Consequences:** Every tool (starting with D-107) must implement and test this boundary.

---

## Phase 1: Prometheus metrics tool

### D-101 `httpx` with `trust_env=False`
- **Context:** The shell exports `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` (college proxy). Observability
  backends are local and must not be reached through, or leaked to, a third-party proxy.
- **Decision:** Use `httpx` (timeouts, streaming, built-in `MockTransport` for tests) and ignore proxy
  environment variables.
- **Consequences:** Pointing `PROMETHEUS_URL` at a remote Prometheus that is *only* reachable through a proxy
  would need an explicit change. A test asserts that the proxy variables are ignored.

### D-102 Read-only by construction: GET `/api/v1/query_range` only
- **Context:** The demo's Prometheus runs with `--web.enable-lifecycle=true`, so `POST /-/quit` and `/-/reload`
  work. The admin API is off.
- **Decision:** A single private `_get()` helper performs the module's only network call. It accepts paths from
  an allow-list containing exactly `/api/v1/query_range` and always uses GET.
- **Consequences:** Very long PromQL (> 2,000 chars, which is rejected anyway) could not use POST form
  bodies; acceptable. Tests assert that `/-/quit`, `/-/reload` and the admin paths are refused.

### D-103 Never raise; return a structured status
- **Decision:** Every outcome is a JSON-serialisable dict with `status` ∈ {`ok`, `no_data`, `error`} and a
  typed `error.type`.
- **Rationale:** A future LLM agent can branch on `status` and read a human-readable `message`; one bad
  query must not crash an investigation loop. `no_data` is not an error: "nothing matched" is evidence.

### D-104 Return summaries, not samples
- **Decision:** Per series: `min`, `max`, `mean`, `first`, `latest` (+ timestamp and age), `change`,
  `change_percent`, `trend`, `early_median`, `recent_median`, `points`, `non_finite_points`. Values are
  rounded to 6 significant digits; series are sorted by `latest` and capped at `max_series` (default 20, with
  `series_count`/`truncated` reported). Bulky resource labels (`host_*`, `os_*`, `telemetry_sdk_*`,
  `service_instance_id`, …) are dropped.
- **Rationale:** Keeps LLM context small and focused. Raw samples would add thousands of tokens of noise.
- **Consequences:** `NaN`/`±Inf` samples (e.g. `histogram_quantile` with no traffic, `0/0`) are counted in
  `non_finite_points`, never summarised, so the output is always strict JSON.

### D-105 Trend = last-quarter median vs first-quarter median, ±10 % of the mean
- **Context:** The first implementation fitted a least-squares line over the whole window (5 % threshold).
  On live data it labelled checkout p95 latency `increasing` while `first→latest` was −25 %, and
  product-catalog rate `decreasing` while it was +0.5 %: the fit was dominated by mid-window shape.
- **Decision:** Compare the medians of the earliest and the most recent quarter of points, relative to the
  series mean, with a 10 % threshold. Return both medians so the verdict can be checked.
- **Consequences:** One spike cannot flip the verdict (a quarter of a 30-minute window is ≥ 7 points), and the
  verdict always agrees with the numbers shown (a property test enforces this on every recorded real
  series). It is a direction label, not anomaly detection; that comes later.

### D-106 Step = max(60 s, window / 240)
- **Context:** Measured sample spacing in this Prometheus is 60 s (OTLP push interval). With a 15 s step, each
  value was repeated 4×, which inflates `points` without adding information.
- **Decision:** Minimum step 60 s (`PROMETHEUS_MIN_STEP_SECONDS`), at most 240 points per series.
- **Consequences:** Short windows have few points (5 min → 6 points). The 7-day max window uses a 42-minute step.

### D-107 Feature-flag isolation inside the metrics tool
- **Context:** Prometheus holds `feature_flag_evaluation_{requests,success}_total` and
  `feature_flag_evaluation_active_count` with labels `feature_flag_key`, `feature_flag_result_variant`, etc.
  (emitted by `cart`). `flagd`/`flagd-ui` appear as `job`, `service_name`, `span_name` and `container_name`
  values. A recorded 422 error message even quoted `job="opentelemetry-demo/flagd-ui"`.
- **Decision (defence in depth):**
  1. Refuse before any request: query text matching `feature flag | flagd | flagservice | openfeature`
     (case-insensitive, including inside strings); any use of `__name__` (blocks name-regex obfuscation such
     as `{__name__=~"feat.+"}`); metric-less selectors like `{job="…"}` (which would sweep flag series into
     counts).
  2. Drop every returned series whose label *names or values* match the pattern.
  3. Redact the pattern from Prometheus error messages and warnings/infos (label sets containing it become
     `{<redacted>}`).
  4. Do **not** report how many series were dropped: that count changes when a flag variant changes, so it
     would itself leak information.
- **Consequences:** Legitimate questions about flagd's health are impossible by design. Residual risk:
  aggregate side channels (e.g. total span volume including flagd calls) remain. The Phase-0 plan for a
  telemetry gateway in front of all backends is the stronger, system-wide control.

### D-108 Tests replay recorded Prometheus responses
- **Decision:** `scripts/record_prometheus_fixtures.py` captures real `query_range` responses (200 matrix,
  empty, NaN, `infos`, 400 `bad_data`, 422 `execution`, flag-bearing series, 28-series selector). Unit tests
  replay them through `httpx.MockTransport`, which also captures every outgoing request so tests can check
  method, path and parameters. Synthetic inputs are used only for transport failures, exact trend shapes and
  oversize bodies. `tests/test_metrics_live.py` runs against the real demo and auto-skips when it is down.
- **Consequences:** The suite is hermetic and fast (< 1 s). Re-record fixtures after upgrading the demo.

### D-109 Default `PROMETHEUS_URL=http://localhost:9090`
- **Evidence:** Compose publishes Prometheus on the fixed host port `9090:9090`. Inside the Docker network the
  address is `prometheus:9090` (`PROMETHEUS_ADDR` in `.env`).

### D-110 No Git commit yet
- **Context:** The project root is not a Git repository (only the cloned `opentelemetry-demo/` is).
- **Decision:** Per instructions, commit only if a repository already exists, so no commit was made. A
  `.gitignore` is provided (ignoring `.venv/`, caches and `opentelemetry-demo/`) for when the repository is
  initialised.

---

## Phase 1b: Observability tool layer (logs, traces, shared sanitization)

### D-201 One network path and one sanitization policy for all tools
- **Decision:** `copilot/tools/_http.py` holds the only HTTP code (GET-only, per-tool path allow-list,
  `trust_env=False`, no redirects, streamed size cap, typed `ToolError`s). `copilot/tools/filters.py` holds the
  only flag policy (pure functions). Every public tool wraps its result in `filters.scrub_output`.
- **Consequences:** A test parses the AST to make sure no other module imports httpx, and another checks that no
  write verb appears in the package. Adding a backend means adding an allow-list, not new HTTP code.

### D-202 `query_metrics` loses its test-only `client` parameter
- **Context:** Agent frameworks such as Google ADK build a tool schema from the function signature; an
  `httpx.Client` parameter would be exposed to the LLM.
- **Decision:** Signature is now `query_metrics(promql, minutes, max_series=20)` (`max_series` bounded 1–200).
  Tests inject recordings by replacing `_http.make_client`. Behaviour is otherwise unchanged; all original
  metrics tests still pass after a mechanical migration.
- **Consequences:** `test_security.py` asserts that every tool uses only str/int/bool/None parameters and
  documents each one under `Args:`.

### D-203 Sanitization policy (evidence-driven)
- **Evidence (OpenTelemetry Demo 3.1.0):**
  - traces carry `feature_flag.evaluation` events with key, variant and value (cart, checkout,
    product-catalog), flagd spans, client spans to `flagd.evaluation.v2.Service` and OFREP, and
    `demo.feature_flag.*` attributes;
  - logs carry Envoy `/flagservice/` access logs and shipping `shipping.feature_flag.evaluated` events;
  - fault code paths emit narration: "Error: Product Catalog Fail Feature Flag Enabled" (span status *and*
    event name), "Feature Flag adManualGc enabled…", "…due to intlShippingSlowdown feature flag",
    "FeatureFlag 'kafkaQueueProblems' is activated/enabled…".
- **Decision:**
  - drop anything from or to the flag service;
  - keep real-work spans but remove flag attributes and events and replace flag-revealing text **as a whole
    string** (partial redaction such as "delayed due to [redacted]" still reveals the cause);
  - drop logs whose message, event name or attribute names concern flags;
  - keep organic error logs that only carry flag text in an attribute value, with that value redacted;
  - refuse flag-related inputs;
  - final recursive scrub of every result.
  - Flag keys are listed explicitly; a test checks the list against `demo.flagd.json`.
- **Consequences:** Symptoms survive. During recorded faults the tools still showed payment error ratio
  0 → 0.5, `payment :: charge` and `product-catalog :: GetProduct` as error origins, product id
  `OLJCESPC7Z`, and frontend HTTP 500s. A redacted value reveals that *something* was withheld, but not
  what.

### D-204 Fixed loopback ports for OpenSearch and Jaeger (`deploy/compose.copilot.yaml`)
- **Context:** Upstream publishes OpenSearch (security plugin disabled) and Jaeger on random host ports bound
  to `0.0.0.0`, i.e. reachable from the college LAN. Jaeger is also reachable via Envoy :8080, but Envoy
  traces every request, so the copilot's own queries would appear as frontend-proxy traffic.
- **Decision:** Overlay with Compose `!override`: `127.0.0.1:9200` and `127.0.0.1:16686` only.
  `scripts/start_demo.sh` runs `make start` and then applies the overlay (`--overlay` re-applies it alone;
  this is idempotent).
- **Consequences:** Stable `OPENSEARCH_URL`/`JAEGER_URL` defaults, and two fewer unauthenticated services on the
  LAN. Applying it recreated those containers once (in-memory traces and that day's log index were lost and
  refilled in minutes). Other demo services, including flagd, are still published on random `0.0.0.0` ports
  upstream (see D-210).

### D-205 `search_logs` design
- `observedTimestamp` for filtering and sorting (shipping/currency send `@timestamp = 1970-01-01`).
- Level is a minimum, matched on `severity.number` **or** normalised `severity.text` (text varies:
  INFO/info/Information; some records have text only; ~40 % have no severity → reported as `unspecified`).
- `GET /<index>/_search` with a JSON body (OpenSearch accepts bodies on GET), so the whole package stays
  GET-only.
- User text goes into a `match` query (never `query_string`): no query-syntax injection.
- Flag exclusions run inside the query (`must_not`), so `total_matches`, `by_service` and `by_level` are
  consistent with the records shown.
- Patterns are built from up to 200 sampled records, with timestamps/UUIDs/hex/IDs/numbers collapsed.
- `_source` is limited to the needed fields (`resource.service.name` instead of ~40 resource attributes).

### D-206 `get_traces` design
- Jaeger requires `service` for search (HTTP 400 otherwise), so `service` or `trace_id` is mandatory.
- Error status is **re-evaluated after sanitization**: most baseline "error traces" contained only flagd
  EventStream timeouts (`errors_only` previously would have blamed fraud-detection, recommendation and ad).
- Error origin = error span with no failing descendant. Relevant spans are ranked by **self time** (duration
  minus the union of child intervals), because total duration always favours the root and proxy wrappers.
- Traces with parent references that Jaeger has not received yet are marked `incomplete` (seen in a recording
  made seconds after the requests). Their origins may be too high in the call chain.
- The tool over-fetches 2× because sanitization can remove whole traces. Percentiles use nearest rank.

### D-207 Fault-state fixtures were recorded with real faults
- **What:** 2026-11-20 20:50–20:59 UTC: `productCatalogFailure` (targeting branch for `OLJCESPC7Z` set to
  "on"; the default variant alone has no effect), `paymentFailure=50%`, `adManualGc=on`,
  `intlShippingSlowdown=5sec`. Restored byte-for-byte from a backup (git shows no diff against tag 3.1.0).
- **Lesson for the future fault harness:** flagd's file sync is `fileinfo` (mtime-based). Restoring with
  `cp -p` kept the old mtime and flagd silently kept the faults active until the file was `touch`ed. Always
  advance the mtime, then verify behaviour (error traces stopped: 0 errors in 7/50/7
  payment/product-catalog/checkout traces after the reload).

### D-208 Test strategy for the tool layer
- An autouse fixture blocks the network and clears all copilot environment variables in unit tests.
- Fixtures are produced by running the real tools through a recording transport
  (`scripts/record_tool_fixtures.py`), plus raw recordings that bypass server-side exclusions to prove the
  client-side defences.
- Cross-tool invariants run over every recorded fixture: no flag text in any output (the corpus is checked to
  really contain flag data), exactly one GET per call, strict JSON, never raising on hostile input.
- Live tests auto-skip when a backend is down; they include trace → logs correlation and a leak sweep over 15
  services.

### D-209 Findings of the final security/correctness review (fixed)
- `httpx.DecodingError`/`InvalidURL` were not `TransportError`s and could escape → mapped to typed errors.
- A malformed but valid-JSON payload could raise from parsing code → last-resort `internal` error in every
  tool.
- `user:pass@` in a configured URL would have appeared in connection-error messages → stripped.
- `traces.py` dropped the useful `error.type` attribute through a prefix match on `error` → exact-key set.
- Log records whose only flag text sat in `exception.message` were dropped client-side after OpenSearch had
  counted them, which created a count/record mismatch (19 vs 8) that hinted at hidden content → they are now
  kept with the value redacted.

### D-210 Accepted residual risks
- Aggregate side channels: volumes that include flag-evaluation work (for example total span counts) still
  move slightly with flag activity. They reveal activity, not which flag or variant.
- The text patterns are heuristics. New demo versions may add new narration, so re-run the fault recordings
  after upgrades.
- flagd and other services remain reachable on random `0.0.0.0` host ports. The tools never contact them;
  future agents must not be given a generic HTTP or shell tool (enforce in the agent phase).
- D-110 still applies: the project root is not a Git repository, so nothing was committed.

---

## Phase 2 (Week 2): Single diagnosis agent and detector

### D-301 Google ADK with plain Python function tools (no MCP)
- **Decision:** The agent is one ADK `LlmAgent`. Tools are passed as plain functions; ADK wraps each one in a
  `FunctionTool` and builds the description Gemini sees from the name, type hints and docstring. The functions run
  in-process.
- **Why not MCP:** MCP is for tools that live in another process or system. Here it would add a server and a
  protocol hop with no benefit. Exposing `copilot/tools` as an MCP server is a possible Week-6 extra, so other
  clients could use the same tool layer.
- **Consequence:** docstrings are now part of the prompt; changing one changes agent behaviour.

### D-302 Health tools that hide PromQL: `get_service_health`, `get_service_metrics`
- **Context:** LLMs often write wrong PromQL (for example `service` instead of `service_name`, or a missing
  `rate()`), get `no_data`, and conclude that nothing is wrong.
- **Decision:** Two thin wrappers run three tested queries (requests/s, error ratio %, p95 ms) through
  `query_metrics`, so they inherit its read-only path, flag filters and never-raise contract. One row per service
  (or per operation), each value as `now` (median of the last quarter of the window), `window_start` (first
  quarter) and `max`, plus plain-language `observations`.
- **Details:** only `SERVER`/`CONSUMER` spans are counted, so each service is measured on the work it receives and
  the load generator's client spans drop out. `errors or 0 * total` gives a healthy service an explicit 0 % instead
  of a gap, so the window start shows the baseline. Observation thresholds: error ≥ 5 %; p95 ≥ 2x window start and
  ≥ 50 ms, only with ≥ 0.05 req/s (fraud-detection at 2 requests a minute produced a false "2.1x latency" note);
  traffic ≥ 2x or ≤ 0.5x.
- **Evidence:** with `paymentFailure=50%` the overview listed `payment` and `checkout` first (≈ 4 KB output);
  `get_service_metrics("payment")` pointed at `oteldemo.PaymentService/Charge`.
- `query_metrics` stays available for anything else (memory, Kafka lag).

### D-303 Prompt in `copilot/prompts/diagnosis.md`, passed as a callable
- The prompt teaches the method (overview, then symptom vs. cause via `error_origins`, then confirm in the suspect),
  includes a PromQL cookbook, and the Week-1 lesson: **search logs with `level="warn"`**, because payment logs its
  failures as warnings.
- The prompt never mentions feature flags or fault switches (a test checks this).
- ADK treats `{name}` in a string instruction as a session-state placeholder. The PromQL examples contain braces, so
  the instruction is passed as a function (`lambda ctx: prompt`), which ADK uses verbatim.

### D-304 Structured answer (`Diagnosis` Pydantic schema)
- Fields: `incident`, `root_cause_service`, `failure_type` (one of 8 values), `confidence`, `summary`,
  `affected_services`, `evidence[{kind, detail}]`, `alternative_causes`, `suggested_next_checks`.
- **Why:** the Week-5 evaluation compares `root_cause_service` and `failure_type` with the injected fault
  automatically; `alternative_causes` gives a top-3 accuracy; `incident=false` scores false alarms.
- With an AI Studio key, ADK cannot combine tools and an output schema natively, so it adds an internal
  `set_model_response` tool that the model calls to hand in the JSON. The tool guard lets that tool through and
  does not count it against the budget (found while reading the ADK 2.15 source; covered by a test).

### D-305 Tool guard: allow-list plus a budget of 15 tool calls
- `before_tool_callback` refuses any tool outside `copilot.tools.TOOLS` (defence in depth: only those are attached)
  and returns a `budget_exhausted` result after 15 calls, telling the model to answer with what it has. This caps
  cost and stops loops. The agent cannot transfer to other agents.

### D-306 Every run leaves a transcript
- `run_diagnosis()` records each tool call, a short view of each result, token usage and latency in a
  `DiagnosisRun`; scripts save it to `runs/<timestamp>-*.json` (git-ignored). Errors such as quota limits are
  captured, not raised, so a failed run still shows how far it got.

### D-307 Detector version 1: fixed threshold, 5-minute smoothing, one alert per incident
- Error ratio per service > 5 % for 120 s, at ≥ 0.01 req/s, polled every 15 s, then a 10-minute cooldown. All
  breaching services go into **one** alert, because failures cascade (payment and checkout breach together).
- **Evidence for 5-minute smoothing:** at ~1 checkout a minute, the 2-minute error ratio of payment jumped between
  0 % and 100 %, and one poll read 0 % while the fault was active. With `[5m]` it read 31–43 % steadily; the live
  dry run fired exactly once, 2 minutes after the breach began.
- The detector reads metrics through `query_metrics` (same filters) and contains no AI. The alert names symptoms
  only. Payment and checkout had the same error ratio, so the alert alone cannot tell cause from symptom; that is
  the agent's job.
- **Version 2 (later):** a learned normal band (rolling mean ± 3σ) so latency faults are caught too.

### D-308 Testing without Gemini
- A scripted fake model (`ScriptedLlm`, a `BaseLlm` subclass) drives the real ADK loop in unit tests: tool calls go
  through ADK to the real tool functions (backends offline), and the answer is parsed. Tests cover both answer
  paths (native schema and `set_model_response`), the budget, unknown tools, model failures and invalid answers.
- No unit test calls Gemini (cost, rate limits and non-determinism). Real runs are manual (`scripts/run_diagnosis.py`)
  and, from Week 5, part of the evaluation.
- A test caught a real bug: `re.match` with `$` accepts a trailing newline (`"payment\n"`), which would have gone
  into PromQL. Fixed with `fullmatch`.

### D-309 Status at the end of the build session
- Built and tested: 427 tests passed (414 unit + 13 live against the running demo).
- **Not yet measured:** Gemini runs. No API key was configured, so the Week-2 target ("finds the payment fault
  most of the time") still has to be checked with `scripts/run_diagnosis.py --runs 5`. No accuracy number may be
  quoted until then.

---

## Phase 3 (Week 3): Knowledge base and RAG

### D-401 Postgres + pgvector in the copilot's own compose file, loopback only
- **Context:** The guide stores runbooks and postmortems in pgvector, and from Week 4 the same Postgres holds
  sessions and incidents. The demo has its own Postgres (`astronomy-db`), but that database is part of the system
  under test.
- **Decision:** `docker-compose.yml` at the project root with one service, `kb-postgres`
  (`pgvector/pgvector:0.8.1-pg17`, pinned), published on `127.0.0.1:5433` with a named volume.
- **Consequences:** The copilot's memory never lives inside the system it diagnoses: a fault in `astronomy-db` cannot
  take the knowledge base down with it. The credentials are local-development defaults (`copilot`/`copilot`) and can
  be overridden. Cloud SQL replaces this container in Week 6.

### D-402 Knowledge content: 14 runbooks and 5 seed postmortems, written for this shop
- **Runbooks:** one per service area, plus "triage: symptom vs. cause" and "known noise and false alarms". YAML
  front matter (`services`, `tags`), then the sections Overview, Symptoms, Diagnosis, Common causes, Remediation and
  Verification.
- **Names checked against the demo:** every metric, span and attribute name in the runbooks was checked against the
  demo source or the recorded fixtures. For example the product ID attribute is `demo.product.id`, not
  `app.product.id`. Other checked names: `container_memory_usage_total_bytes`, `postgresql_backends`,
  `publish orders`, `valkey-cart:6379`. A wrong name in a runbook would send the agent to `no_data`, which is the
  D-302 trap ("no data, so nothing is wrong").
- **Postmortems are made up.** `kb/README.md` says so; the documents themselves read like real write-ups.
- **Overlap with the faults we will inject (deliberate, recorded only here, never in `kb/`):**
  - "seen before": payment charges rejected (like `paymentFailure`), recommendation cache leak (like
    `recommendationCacheFailure`), orders queue backlog (like `kafkaQueueProblems`);
  - related but different: cart store unavailable. Its root cause is `valkey-cart`, while the injectable fault is in
    the cart service. It teaches "the service with the most errors is not the cause";
  - false alarm: supports the no-fault scenario;
  - no postmortem: `productCatalogFailure`, `productCatalogLockContention`, `loadGeneratorFloodHomepage`,
    `paymentUnreachable`, `adFailure`, `adHighCpu`, `adManualGc`, `emailMemoryLeak`, `imageSlowLoad`,
    `intlShippingSlowdown`, `cartFailure`.

  Week 5 can therefore report accuracy separately for "seen before" and "runbook only" faults.
- Remediation is written in real-world terms ("roll back the most recent configuration change"). Week 4's
  approval-gated action tool maps that to switching the fault off.

### D-403 No fault-injection terms in the knowledge base (three layers)
- Ingestion refuses any document with a line matching the telemetry filter patterns (`filters.mentions_flags`) and
  reports `file:line`.
- A unit test scans every `kb/*/*.md` file for a broader pattern: even the word "flag" is absent.
- The tools refuse flag-related queries before searching and pass their output through `scrub_output`, like the
  telemetry tools.
- **Why:** a runbook that said "switch off paymentFailure" would hand the agent the answer and make every evaluation
  meaningless.

### D-404 Chunking: one chunk per `##` section, with its title and heading as context
- A section ("Symptoms", "Diagnosis", "Remediation") is the unit an engineer actually needs. Whole files would dilute
  the match and cost tokens. Fixed-size windows would cut steps in half.
- The text that gets embedded is `title + heading + section text`, so a section still makes sense on its own. Text
  before the first `##` becomes "Overview". Sections over 1,500 characters are split at blank lines, never inside a
  code block.
- Result: 19 documents give 116 sections (81 runbook, 35 postmortem). IDs such as `runbook/payment#05-remediation`
  are stable.

### D-405 Gemini embeddings: `gemini-embedding-001`, 768 dimensions, separate task types
- Sections use `RETRIEVAL_DOCUMENT` and queries use `RETRIEVAL_QUERY`. The model is trained for this asymmetric case
  (a short question against a paragraph).
- `output_dimensionality=768` truncates the 3072-dimension default (Matryoshka training makes this safe). The table is
  4 times smaller and stays under pgvector's 2,000-dimension index limit. Truncated vectors are not unit length, so
  they are L2-normalised.
- The model is configurable (`KB_EMBEDDING_MODEL`). Each row stores the `embedding_model` that produced it. Vector
  search compares only rows from the query's model (vectors from different models are not comparable), and ingestion
  re-embeds rows when the model changes.
- Query embedding makes one attempt with a 10 s timeout and has a 256-entry cache. Document embedding sends batches of
  50 and retries rate limits, server errors and timeouts (back-off of 2, 4 and 8 s).
- Data exposure: query text is sent to Google, the provider that already receives every prompt and tool result.

### D-406 Hybrid retrieval: full text + pgvector, fused with RRF, grouped by document
- **Keyword:** `plainto_tsquery` (stems, drops stop words, ignores operator syntax), with its ANDs rewritten to ORs.
  A description of symptoms should rank documents by shared words, not require every word. `ts_rank` weighs
  title/heading (A) above services/tags (B) and body text (D).
- **Vector:** exact cosine distance (`<=>`) with no approximate index. 116 rows take about a millisecond, and HNSW
  combined with WHERE filters can return fewer rows than asked. Add the index above roughly 10,000 rows.
- **Fusion:** Reciprocal Rank Fusion with k = 60 (Cormack et al., 2009) uses only ranks, so a `ts_rank` score and a
  cosine similarity never have to be put on the same scale.
- **Grouping:** the result is k documents, each with its two best sections (truncated at 1,200 characters). For 4
  runbooks that is about 7 KB.
- **Why hybrid:** keyword search misses paraphrases (measured in D-411). Vectors alone are weaker on exact names
  (`oteldemo.PaymentService/Charge`, `valkey-cart`) and cannot say why something matched. Production RAG systems
  commonly use both.

### D-407 The knowledge base must never block an investigation
- No key: ingestion stores text only and search runs keyword-only (`retrieval: "keyword"`).
- An embedding failure at query time falls back to keyword search with a `warnings` entry.
- If nothing is embedded with the current model, search falls back to keyword with a warning and skips the
  query-embedding call, so no API call is wasted.
- If the database is down, the tool returns `status: error` (`connection`) with the start command, and the agent
  continues with telemetry (the prompt tells it to).
- The explicit modes `hybrid` and `vector` raise instead of falling back, so a silent fallback can never distort a
  measurement.

### D-408 Read-only search path, single writer, schema-qualified SQL
- Search sessions run with `default_transaction_read_only=on`, a 5 s `statement_timeout` and autocommit. A test checks
  that a `DELETE` fails with `ReadOnlySqlTransaction`.
- Only `copilot/kb/ingest.py` contains write SQL. A test scans `store.py`, `search.py`, `embeddings.py` and
  `tools/kb.py` for INSERT, UPDATE, DELETE and DDL.
- All values are query parameters. Hostile queries (`'; DROP TABLE ...`, tsquery operators, `%(query)s`) are tested
  against the real database.
- **Bug found while writing tests:** with unqualified table names, `--rebuild` in an empty `KB_SCHEMA` would have
  resolved through `search_path` to `public.kb_chunks` and dropped the real table. Every statement now names
  `<schema>.kb_chunks`.
- **Residual risk:** read-only is a session default that a session could switch off. The code never does, and the
  LLM cannot send SQL. A separate read-only database role is the Week-6 hardening for Cloud SQL.

### D-409 Incremental, all-or-nothing ingestion (`python -m copilot.kb.ingest`)
- Each section has a content hash. Only new or changed sections are embedded, so re-running on an unchanged
  knowledge base makes zero API calls (tested).
- Embeddings are computed before any write, and upserts and deletes run in one transaction. A failed embedding run
  leaves the database untouched (tested).
- Text can be stored now and embedded later: adding the key and re-running ingestion embeds all 116 sections once.

### D-410 The diagnosis agent gets the knowledge tools only with `knowledge=True`
- The default agent's tools and prompt are unchanged, so the Week-2 "single agent, no RAG" baseline (the guide's
  ablation in Figure 8) can still be measured.
- `knowledge=True` (or `scripts/run_diagnosis.py --knowledge`) gives 7 tools and appends
  `copilot/prompts/diagnosis_knowledge.md`. It tells the agent to search after the overview, that knowledge is not
  evidence, and to list `references`.
- The new optional `Diagnosis.references` (default empty) is the only change the default agent can see.
- No new agent this week. The Knowledge (RAG) agent comes with the Week-4 split into a Coordinator and sub-agents.

### D-411 Retrieval check (measured) and its limits
- `eval/retrieval_queries.yaml`: 28 queries (20 that share words with the right document, 8 paraphrases), each with
  the documents that count as correct. Run with `scripts/check_retrieval.py --mode keyword|vector|hybrid`.
- **Measured 2026-11-27** (`gemini-embedding-001`, 768 dimensions; files in `runs/*-retrieval-*.json`):

  | Mode | All: hit@1 / hit@3 / MRR | Keyword-style (20): hit@1 / hit@3 | Paraphrases (8): hit@1 / hit@3 / MRR |
  |---|---|---|---|
  | keyword | 0.86 / 0.93 / 0.90 | 1.00 / 1.00 | 0.50 / 0.75 / 0.66 |
  | vector | 0.96 / 1.00 / 0.98 | 1.00 / 1.00 | 0.88 / 1.00 / 0.94 |
  | hybrid (default) | 0.93 / 1.00 / 0.96 | 1.00 / 1.00 | 0.75 / 1.00 / 0.85 |

- **Week-3 done criterion met in every mode:** "checkout errors" returns the checkout runbook at rank 1 (target: top
  3). "payment charge errors" returns the payment runbook first.
- **What embeddings changed:** both paraphrase misses of keyword search ("customers cannot pay for their order",
  "the suggestions service uses more and more RAM") are found in the top 3.
- **Observation, reported as measured:** vector-only puts the right paraphrase document first once more than
  hybrid (7/8 vs. 6/8). Keyword matches pulled two paraphrases down ("customers cannot pay…" rank 1 to 3,
  "shoppers get an error…" rank 1 to 2) and lifted one ("fraud checks complete very late" rank 2 to 1).
- **Decision: keep hybrid as the default and do not tune on this set.**
  - The difference is one query out of 8, and hit@3 is identical (1.00).
  - The agent reads the top 3–4 documents, not only the first.
  - Real agent queries contain exact names (`oteldemo.PaymentService/Charge`, `valkey-cart:6379`) that this set does
    not test.
  - Re-weighting RRF to win on these 8 queries would be fitting the evaluation. The deciding measurement is the
    agent-level ablation (Week 5), where the mode can be switched.
- **Limits:** the same person wrote the documents and the queries, after reading the documents. This is a regression
  check, not a benchmark.

### D-412 Test strategy for the knowledge base
- Unit tests are hermetic: no database (connections blocked unless a test is marked `db`) and no Gemini (conftest
  replaces `make_embedder`). A fake google-genai client tests batching, task types, retries and error mapping. An
  in-memory store tests fusion, grouping and fallbacks.
- `db` tests run against the real Postgres. Each runs in a private schema (`kb_test_<random>`) that is dropped
  afterwards, and they are skipped when the database is down. An offline `HashingEmbedder` exercises the pgvector
  path without network access.
- **Status:** 584 tests = 551 unit + 20 db + 13 live. 571 passed. The 13 live tests were skipped because the demo was
  not running. That is 157 new tests (427 before).
- **Not yet done:** agent runs with `--knowledge` (and the Week-2 runs without it). They need the demo running with a
  fault switched on.

### D-413 Gemini free tier in practice: per-minute limits, a new key format
- **Rate limit on the first real ingestion:** embedding the 116 sections in batches of 50 got HTTP 429. The short
  back-off (2, 4, 8 s) gave up inside the same minute, and, as designed, nothing was written. Single requests
  succeeded right after, so the limit is per minute, not per day.
- **Fix:**
  - Wait as long as the API's `RetryInfo` asks (capped at 65 s), with up to 5 retries.
  - Report a *daily* quota (a quota ID containing `PerDay`) at once as `quota_daily` instead of retrying.
  - Print a progress line while waiting.
  - The second run embedded all 116 sections in 24 s, including one 17 s wait.
- **Evaluation queries are embedded in batches:** `warm_queries` embeds them before the run and caches them. The
  retrieval check makes one request per 50 queries, so rate limits cannot turn into missing results.
  Investigation-time queries stay single-attempt, falling back to keyword search.
- **Key format:** the AI Studio key is in the newer `AQ.` format (53 characters), not the classic `AIza…` (39). Both
  work with google-genai. Error messages now remove both formats, and tests use obviously fake keys so GitHub secret
  scanning has nothing to flag.

---

## Phase 4 (Week 4): Agent team, human approval, Postgres state, tracing

### D-501 Five specialist agents; the coordinator is code, not an LLM
- **Agents** (ADK `LlmAgent`s, `copilot/agents/team.py`), each with one job, a tool budget and a structured output:

  | Agent | Job | Tools (budget) | Output |
  |---|---|---|---|
  | triage | what is broken, how badly | the 5 telemetry tools (6) | `TriageReport` |
  | knowledge | have we seen this before | `search_runbooks`, `search_postmortems` (4) | `KnowledgeReport` |
  | diagnosis | why is it broken (ReAct) | all 7 read tools (15) | `Diagnosis` |
  | reviewer | is the evidence solid | none | `Review` |
  | action | what should we do | `rollback_config`, `restart_service`, `escalate_to_human` (1) | one tool call |

- **Coordinator** (`copilot/agents/coordinator.py`): a fixed procedure in code. Triage, then knowledge, then
  diagnosis and reviewer in a loop (at most 2 rounds), then action, then a pause for a human.
- **Why code:** incident response has a known procedure, and the guarantees matter more than flexibility:
  - review always comes before any action;
  - the run always stops at the same place for a human;
  - every step has a budget;
  - a failed step leaves a record;
  - every evaluation run has the same structure.

  ADK's LLM-driven transfer (a coordinator `LlmAgent` with `sub_agents`) could skip the reviewer or act early. The
  LLMs decide *within* steps (what to query, what the evidence means, what to propose).
- Each agent receives the earlier agents' outputs as JSON sections in its message, so no prompt templating is
  needed. The diagnosis agent gets an extra prompt section on using the triage report, the knowledge report and the
  reviewer's questions (`diagnosis_team.md`). With `team=False` and `knowledge=False` it is still the Week-2 agent.

### D-502 The write path is a separate package; the practice rollback stays hidden
- `copilot/actions/` holds the only code that changes anything. `copilot/tools` stays read-only and never imports it
  (AST test). Only the action agent gets these tools (test).
- **`rollback_config(service)`** restores that service's fault-injection settings to their healthy values ("off").
  Targeting rules keep their structure; only the variants they return change. This is the practice environment's
  stand-in for a real rollback (Git revert, config-store version, deployment rollback):
  - the service-to-settings mapping lives only in `remediation.py`;
  - results talk about the service ("rolled back to its last known good version"), never the settings;
  - results pass through `scrub_output`;
  - no prompt or docstring mentions the mechanism (tests).
- `restart_service` runs `docker restart -t 10 <service>`: an argument list, never a shell, limited to the 18 shop
  services. flagd, the databases and the observability stack cannot be restarted.
- `escalate_to_human` changes nothing.

### D-503 Two locks on every change
1. **ADK tool confirmation.** `FunctionTool(..., require_confirmation=True)` pauses the run with an
   `adk_request_confirmation` event. Nothing runs until a confirmation response arrives.
2. **The tool checks for itself.** Each write tool refuses to run unless its `tool_context` carries a *confirmed*
   approval, so a direct call from code cannot bypass the human either (tested with no, rejected and empty contexts).

Further rules:
- One action per incident (budget 1).
- **Finding (ADK 2.15 source, `flows/llm_flows/tools/_caller.py`):** `before_tool_callback` runs *before* the
  confirmation gate, both when a call first pauses and again when the approved call resumes. A guard that counted
  every invocation would have refused the approved action as "over budget". The new `tool_guard` counts each
  function-call ID once.
- Audit trail: every action call is appended to `runs/actions/audit.jsonl` (approver, incident, args, result), and a
  rollback first backs up the settings file to `runs/actions/backups/`. `COPILOT_ACTIONS_DRY_RUN=1` reports without
  changing anything.
- The settings file is written to a temporary file and renamed, and its modification time always moves forward. The
  demo mounts the directory, so a rename is visible in the container, and D-207 showed that flagd ignores a file
  whose mtime did not change.

### D-504 Pause and resume: one ADK session per step, stored in Postgres
- Each step runs in its own ADK session (`<incident>-triage`, `<incident>-diagnosis-1`, `<incident>-action`, ...).
  Each agent sees only its own conversation plus the JSON sections it is given (no mixing of agents' histories).
- `DatabaseSessionService` stores sessions in the kb-postgres database, in its own schema `adk`. SQLAlchemy uses the
  psycopg 3 driver in asyncio mode (`postgresql+psycopg://`), so no second Postgres driver is needed. ADK requires an
  async driver and refuses a synchronous URL. `COPILOT_SESSIONS=memory` keeps everything in memory.
- `decide()` answers the confirmation request in the stored action session (a function response named
  `adk_request_confirmation` with `{"confirmed": ..., "payload": {"by", "note", "incident_id"}}`). ADK finds the
  paused call, re-runs it if approved, and the agent closes with one sentence.
- This works from **another process, hours later**: a test resumes with a new coordinator and a fresh
  `DatabaseSessionService` that share nothing but Postgres.
- `ResumabilityConfig` is not needed: each paused step is a single root `LlmAgent`, which ADK's node runtime resumes
  from the function response.

### D-505 Incident records with an exactly-once approval
- Table `<KB_SCHEMA>.incidents`: `id`, `status`, `alert`, the summary columns `root_cause_service` and
  `failure_type` (for listing), timestamps, and `record` as JSONB. The record holds the timeline, every agent output,
  every review with its grounding check, the proposal, the decision, the action result, the verification and the
  per-step tokens, latency and errors.
- Statuses: `investigating` → `awaiting_approval` → `approved`/`rejected` → `fix_applied` → `resolved`. Other
  endings: `no_incident`, `needs_human`, `failed`.
- The approval is a compare-and-set (`UPDATE ... WHERE status = 'awaiting_approval' RETURNING`). Two simultaneous
  approvals: exactly one wins (threaded test against real Postgres).
- The record is saved after every step, so a crash mid-investigation still leaves what was done.

### D-506 Reviewer = an LLM for reasoning + code for facts
- `copilot/agents/review.py` compares the diagnosis with the **full** tool results of the investigation (triage plus
  every diagnosis round).
- **Hard checks:**
  - internal consistency (incident vs. `none`);
  - every cited 32-hex trace ID must appear in a tool result;
  - the root-cause service must appear in a tool result.
- **Soft checks:** evidence count, confidence vs. kinds of evidence, and the share of evidence items "anchored" by a
  trace ID, a decimal or 3+-digit number, or quoted text found in the results. The anchored share is a rough
  evidence-grounded rate for Week 5; it shows that a cited value occurs, not that it supports the claim.
- A hard failure forces `revise` whatever the Reviewer LLM says. Its issues and questions go back to the diagnosis
  agent in the next round.
- After 2 rounds without approval the incident is `needs_human`, and the action agent is never called.

### D-507 Act only on approved diagnoses of real incidents
- Action runs only if the review approved **and** `incident=true`.
- An approved `incident=false` ends as `no_incident`.
- If the action agent escalates or proposes nothing, the incident ends as `needs_human`.
- The action agent receives the diagnosis, the knowledge agent's remediation hints and the list of services it may
  act on.

### D-508 Postmortem drafts are generated from data and never ingested automatically
- `copilot/postmortem.py` builds the draft from the record (timeline, root cause, evidence, review, proposal,
  decision, result, verification). There is no LLM in this step, so every line traces back to data. Impact,
  severity and lessons are marked TODO for a human.
- Drafts go to `runs/postmortems/` (git-ignored) and are valid knowledge documents (tested with `parse_document`),
  but they are **not** added to `kb/` automatically:
  - postmortems are reviewed documents;
  - automatic ingestion would let evaluation runs feed their own answers into the knowledge base. Five payment runs
    would create five payment postmortems.

### D-509 Tracing the copilot, in its own Jaeger
- ADK already creates spans for agent runs, LLM calls and tool calls (model, tokens, tool names). The coordinator
  adds an `incident` span, one `step <agent>` span per step and a `decision` span, so one incident is one trace.
- `setup_tracing()` exports via OTLP/HTTP only when `COPILOT_OTLP_ENDPOINT` is set, to `copilot-jaeger` (new in
  `docker-compose.yml`: UI on 16687, OTLP on 4328, the same Jaeger image as the demo).
- **Never the demo's collector:** its span-metrics pipeline would turn the copilot's own traffic into a service
  "incident-copilot" in Prometheus. `get_service_health` would then show the copilot's LLM latency to its own
  agents. `setup_tracing` refuses the collector ports 4317/4318.

### D-510 Recovery check only where it can be honest
- `decide(..., verify_minutes=N)` polls `get_service_health` until the root-cause and affected services are below
  5 % errors in two polls in a row. Only then is the incident `resolved`.
- Only `errors`/`unavailable` faults are verified this way. For latency, resource or queue faults the error ratio
  stays near 0 % while the problem continues, so a check would report success falsely. The result says a human must
  check instead.

### D-511 Tests: written first, then run (2026-11-28)
- New tests:
  - `test_actions.py`: approval lock, rollback, restart, audit, no leaks;
  - `test_review.py`;
  - `test_team.py`: tools per agent, guard, `run_step`, ADK pause and resume with approve/reject;
  - `test_coordinator.py`: full incidents with one scripted model per agent, the review loop, forced revision,
    every ending, exactly-once decisions, resume by a new coordinator, verification;
  - `test_state_db.py` (`db`): records, the approval race, resume through a fresh Postgres session service;
  - security additions: the tools never import actions, no prompt mentions the mechanism.
- Test safety (conftest): the settings path points at a missing file, action logs and drafts go to `tmp_path`, and
  the docker call is blocked unless a test replaces it. A test can never touch the real demo.
- **Result:** 675 tests (639 unit, 23 db, 13 live) all passed on the first run. The unit and db suites were re-run
  after every change below and kept passing. The final count is 678 (642 unit, 23 db, 13 live), all passing. The
  live runs are in D-516.

### D-512 ADK 2.15 behaviour confirmed in the source (not assumed)
- For a single paused call, the confirmation request event is emitted *before* the tool's placeholder result
  (`core/_function_call_postprocessor.py`). The runtime recognises the placeholder by the event's
  `requested_tool_confirmations`, not by order.
- An agent with an `output_schema` validates its final text itself and raises on non-JSON
  (`LlmAgent.__maybe_save_output_to_state`). `run_step` records that as the step's error.
- A paused call does not re-invoke the model (`skip_summarization`), and the invocation pauses on the
  `adk_request_confirmation` call.
- `tool_context` is injected by name and is not in the schema the model sees (tested).


### D-513 The Gemini free tier: 5 requests a minute, 20 a day, per model
- **Observed on the first live run:** `gemini-2.5-flash` on the free tier allows 5 generate requests per minute
  (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier`, 5). The team hit HTTP 429 in the diagnosis step after
  30 s, and the incident correctly ended as `failed`.
- **Second run:** 20 requests per **day** (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, 20, retry in
  42,810 s). Two investigations used up the whole day.
- **Fix 1, pacing and retries** (`copilot/agents/llm.py`):
  - a process-wide pacer (`COPILOT_LLM_RPM=5`, an ADK `before_model_callback` on every team agent) waits *before*
    a request instead of failing after it;
  - ADK's `Gemini` model is built with `retry_options` (429/5xx, 10-60 s back-off) for what pacing does not prevent;
  - pacing time is recorded per step (`rate_limit_wait_seconds`) and as a span attribute, so waiting is not
    mistaken for slowness.
- ADK logs every model error with a full traceback (4 per failed step). `scripts/incident.py` hides them unless
  `--debug` is given; the record keeps the error.
- **Model choice:** quotas are per model. `gemini-2.5-flash-lite` is no longer available to new users (HTTP 404);
  `gemini-3.5-flash` and `gemini-3.5-flash-lite` are, each with its own quota. On this key `gemini-3.5-flash` is
  **served by `gemini-3.6-flash`**: the 429 names 3.6-flash, so the two share one daily quota of 20. The default stays
  `COPILOT_MODEL=gemini-2.5-flash`. The runs after the daily limit used `gemini-3.5-flash` and
  `gemini-3.5-flash-lite` (set per command, not in `.env`).

### D-514 Fewer model calls per incident (prompt and structure version 2)
Within a 20-requests-a-day quota, model calls are the cost that matters. Run 2 spent 12 calls before reaching the
action step. The transcript showed where:

| Waste seen in run 2 | Change | Effect in run 3 |
|---|---|---|
| Knowledge: one turn to call two searches, one turn for the JSON, one malformed call (`SetModelResponseRunbooks`, a tool that does not exist) | Retrieval moved into code (the triage query, 3 runbooks + 2 postmortems); the agent has no tools and answers in one call | 3 calls, 197 s -> **1 call, 11 s** |
| Diagnosis repeated triage's `get_service_health` and `get_traces(checkout)` | Prompt: start from the suspects, do not repeat triage's calls, batch independent calls in one turn; budget 15 -> 10 | 5 calls, 66k tokens -> **2 calls, 26k tokens** |
| Triage used one turn per tool | Prompt: health and the named service's traces in the same turn; budget 6 -> 4 | health + traces + logs in 2 turns |
| After an approved action ADK asks the model to summarise the tool result | `after_tool_callback` sets `skip_summarization`: the tool result is the answer | approval costs **0 calls** |

Result: the whole incident took **8 model calls**, 61k tokens and 81 s, against 12 calls, 99k tokens and 359 s
for the part of run 2 that completed. The model also differs between the runs (D-513): the call count comes from
the structure and budgets, while tokens and quality also depend on the model.

### D-515 Evidence must match confidence, and revisions must keep evidence (prompt version 3)
- **Observed (run 4, `gemini-3.5-flash-lite`):** the diagnosis was correct (payment, errors), but the reviewer
  rejected it twice and the incident ended as `needs_human`.
  - Round 1: confidence high with only metric and log evidence.
  - Round 2: asked for traces, the diagnosis *replaced* its log evidence with trace evidence, so it still had only
    two kinds.

  The reviewer was right by the rules (high = metric, trace and log). The safety rule held: no action without an
  approved review.
- **Fix:**
  - `diagnosis_team.md` v3 states the rule (high = one metric, one trace and one log item; medium = two kinds; else
    lower the confidence) and says to keep earlier evidence when revising;
  - `reviewer.md` v2: when the only problem is the confidence label, offer both fixes (add the missing kind or lower
    the confidence) instead of a new investigation.
- **Result (run 5, same model, same fault):** approved in round 1. 8 calls instead of 11, 57k tokens instead of 75k.

### D-516 Live results, 2026-11-28 (paymentFailure 50 %, unless noted)
| Run | Version / model | Outcome | Model calls | Tokens | Agent time |
|---|---|---|---|---|---|
| 1 | v1, 2.5-flash | failed: 429 per minute in diagnosis (after 30 s) | not counted yet | 36k | 31 s |
| 2 | v1 + pacing, 2.5-flash | correct diagnosis (payment, errors, high), review approved; failed: 429 per **day** at action | 12 | 99k | 359 s |
| 3 | v2, 3.5-flash | correct; approved in round 1; `rollback_config(payment)` approved by a human; **resolved** (verified 2 min later) | 8 | 61k | 81 s |
| 4 | v2, 3.5-flash-lite | correct diagnosis; review not approved in 2 rounds -> `needs_human`, no action (D-515) | 11 | 75k | 123 s |
| 5 | v3, 3.5-flash-lite | correct; approved in round 1; proposal **rejected** by a human; nothing changed | 8 | 57k | 66 s |
| 6 | v3, 3.5-flash (= 3.6-flash) | **false alarm** (healthy system, alert on fraud-detection 25 %): `no_incident`, high confidence, review approved, no action | 13 | 117k | 182 s |
| 7, 8 | v3 + fix (D-517), 3.5 / 3.6-flash | daily quota used up; run 8 stopped after one step | 0 | 0 | 222 s / 75 s |
| 9 | v3 + fix (D-517), 3.7-flash | **same false alarm**: `no_incident`, high confidence, review approved | **9** | **60k** | 718 s* |

- **Every completed diagnosis named the root cause correctly:** payment, failure type errors, with checkout and
  frontend as symptoms. The agents also found *which* charges fail: those of gold-loyalty users, with
  "Invalid token" in the payment logs.
- **Run 3:** alert to diagnosis 71 s, reviewed proposal at 83 s, recovery confirmed 2 minutes after the approval.
- **Grounding:** every approved diagnosis had all its evidence items anchored in real tool results, and every
  cited trace ID existed.
- **The knowledge agent filtered the retrieval:** of 3 runbooks and 2 postmortems retrieved, it kept the payment and
  checkout runbooks and the payment postmortem, and described how the past incident differs.
- **Nothing changed before a human approved:** in every paused run the settings file still read 50 %. After the
  approval in run 3 it was byte-identical to the upstream file again.
- **The approval was resumed by a separate command:** a new process with a new Postgres session service, with no
  model call.
- **Tracing:** each incident is one trace in copilot-jaeger (27-40 spans), and the demo's Jaeger has no
  `incident-copilot` service.
- **False alarms are handled, and the knowledge base helps:** for the fraud-detection false alarm, retrieval put the
  known-noise runbook and the false-alarm postmortem first. Both runs concluded "no incident" with the right reason:
  a low-traffic error ratio (about 0.07 requests a second).
- \* Run 9's wall time came from the model: one knowledge call took 262 s, with no time in the pacer. Calls and
  tokens are the comparable numbers.
- **Limits:** two scenarios (one fault type and one false alarm), nine runs, several models (forced by the quotas)
  and self-written alerts. It is a smoke test of the system, not an accuracy measurement; that is the Week-5
  evaluation.

### D-517 Proving a negative cheaply, and stopping at a used-up daily quota
- **Observed (run 6):** a false alarm cost the most of any run: 13 model calls, 16 tool calls, 117k tokens.
  - Triage asked for 6 tools (its budget of 4 refused 2).
  - Diagnosis used all 10 of its calls, checking payment, checkout and 30-minute windows to show that nothing was
    wrong.
- **Fix:**
  - `triage.md` v3: when the overview shows only noise, check just the alerted service, then answer
    `incident_likely: false`;
  - `diagnosis_team.md` v4: proving that nothing is wrong needs one check per signal for the alerted service;
  - the coordinator now tells the diagnosis agent when triage saw no sign of an incident and asks for a quick
    check.

  The guard budget stays the hard cap.
- **Result (run 9, same alert):** still `no_incident` with the right reason. **9 calls instead of 13, 11 tools
  instead of 16, 60k tokens instead of 117k (-49 %).** The model differs (3.7-flash).
- **Observed (run 7):** after a *daily* 429 the coordinator kept going. Each step sat through the client's retries
  and failed, 222 s for nothing.
- **Fix:** a step error naming a `PerDay` quota raises `DailyQuotaExhausted`. The investigation stops at once with a
  clear message ("set COPILOT_MODEL to another model, or wait"). Run 8 stopped after one step (75 s), and a unit
  test checks that no later agent is called.

