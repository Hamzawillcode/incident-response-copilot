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
