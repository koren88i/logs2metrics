# Logs2Metrics — Project Changelog

> Historical record of completed phases, bug post-mortems, and lessons learned.
> For active coding standards see [CLAUDE.md](CLAUDE.md). For architecture see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## High-Cardinality Filter Field Awareness (2026-02-25)

Added detection and warnings for high-cardinality fields used in dashboard filters. When a Kibana dashboard has filter controls (e.g., dropdown for `user_id` or `client_ip`) or panels with structured filters on high-cardinality fields, the scoring engine now surfaces amber warnings explaining that converting those panels to metrics will lose drill-down capability by those fields.

- **New**: `_extract_filter_fields()` helper in `kibana_connector.py` — parses `searchSourceJSON.filter[]` structured filters to extract field names (handles `meta.key`, `range`, `match_phrase`, `exists` shapes)
- **New**: `_extract_control_panel_fields()` helper — extracts field names from dashboard control panels (`optionsListControl`, `rangeSliderControl`, `controlGroup`, legacy `input_control_vis`)
- **New**: `filter_fields` field on `PanelAnalysis` model — panel-level structured filter fields
- **New**: `dashboard_filter_fields` field on `DashboardDetail` model — dashboard-level control and filter fields
- **New**: `warnings` field on `SuitabilityScore` model — informational warnings (separate from numeric score)
- **New**: `_check_filter_field_warnings()` in `scoring.py` — checks filter fields against `HIGH_CARDINALITY_FIELDS` set
- **UI**: Panel cards now show `Filters:` metadata; dashboard summary shows `Dashboard filters:`; amber warning boxes appear below score breakdown when high-cardinality filter fields detected
- **Tests**: 25 new tests (221 total) covering filter field extraction, control panel parsing, and warning generation

---

## External Dashboard Parsing Fixes (2026-02-16)

Bug hunt session: connected the portal to a real external Kibana (`elastic_recommand` project) and tested every panel type. Found and fixed several parsing gaps that only manifest with non-time-series panels.

- **Fix**: Kibana `schema: "bucket"` not handled in panel parsing
  - **Symptom**: Table panels (e.g. "Top Queried Fields") parsed with `group_by_fields: []` and `es_query.aggs: {}`. Preview Agg returned no results; Create Rule would create a transform with no dimensions.
  - **Root cause**: Kibana uses three schemas for bucket aggs: `"segment"` (x-axis), `"group"` (split series), `"bucket"` (table rows). Our code only handled `"segment"` and `"group"`, silently dropping all table-row terms aggs.
  - **Fix**: Added `"bucket"` to the schema check in both `_parse_visualization()` and `_build_es_query_from_vis_aggs()`.

- **Fix**: Kibana `schema: "segment"` terms aggs dropped for pie/bar charts
  - **Symptom**: Pie panels (e.g. "Query Type Distribution") parsed with `group_by_fields: []` and `es_query.aggs: {}`. Same empty results as the table bug.
  - **Root cause**: Our code assumed `schema: "segment"` always meant `date_histogram`. Pie and bar charts use `"segment"` for their terms slices/buckets too. Non-date_histogram segment aggs were silently ignored.
  - **Fix**: In `_parse_visualization()`, non-date_histogram segment aggs are now treated as group-by dimensions. In `_build_es_query_from_vis_aggs()`, segment terms are routed to `group_aggs` instead of being ignored.

- **Fix**: Preview Agg had no time range filter
  - **Symptom**: Preview aggregated over the entire index history instead of the dashboard's time range. Results didn't match what Kibana showed.
  - **Root cause**: Kibana silently injects a time range filter (e.g. `timestamp >= now-24h`) based on the time picker. Our preview query used the panel's `es_query` as-is with no time constraint.
  - **Fix**: Preview Agg now reads the Lookback dropdown and wraps the query in a `bool` + `range` filter on the panel's `time_field`.

- **Fix**: Search proxy defaulted to `app-logs` index
  - **Symptom**: If a caller forgot to pass `index` in the request body, the proxy would silently query `app-logs` instead of returning an error. On external systems without `app-logs`, this caused `index_not_found_exception`.
  - **Fix**: `POST /api/es/search` now returns 400 if `index` is not provided. No silent fallback.

- **Fix**: `time_field` not resolved for panels without `date_histogram`
  - **Symptom**: Table/pie panels had `time_field: null`, which would default to `'timestamp'` in the UI — potentially wrong for the target index.
  - **Root cause**: `time_field` was only extracted from `date_histogram` columns. Panels without date_histogram never populated it.
  - **Fix**: New `_extract_index_and_time_field_from_refs()` resolves `timeFieldName` from the Kibana data view API. All three panel parsers (`_parse_visualization`, `_parse_lens_panel`, `_parse_embedded_panel`) fall back to the data view's time field when no date_histogram is present.

---

## ES Connection Routing for External Clusters (2026-02-15)

- **Feature**: Added `X-ES-Url` header support — allows the portal UI to target any Elasticsearch cluster for all operations (analysis, transforms, provisioning, status checks)
  - New ES URL input field in connection bar (next to Kibana URL), sent as `X-ES-Url` header
  - Resolution priority: explicit `X-ES-Url` → service map lookup from `X-Kibana-Url` → default `ES_URL` env var
  - Supports optional `X-ES-User` / `X-ES-Pass` headers for authenticated ES clusters
  - New `get_es_client()` FastAPI dependency replaces the old `_get_es_client()` helper
- **Feature**: Threaded `es_client` parameter through the entire call chain
  - `es_connector.py`: all 4 functions accept `es_client=None`
  - `cost_estimator.py`: `estimate_cost()` and `_estimate_series_count()` accept `es_client=None`
  - `guardrails.py`: `evaluate()` accepts `es_client=None`
  - `analyzer.py`: `analyze_dashboard()` and `_resolve_field_types()` accept `es_client=None`
  - `backend.py` ABC: all 4 abstract methods accept `es_client=None`
  - `elastic_backend.py`: all public methods accept `es_client=None`, private helpers take `client` as required param
- **Feature**: Added "Skip — Use Existing Data" button in Pipeline Step 1 — unlocks analysis steps without generating synthetic logs, enabling analysis of external dashboards with real data
- **Fix**: `/api/config` now returns `es_url` alongside `kibana_url` for UI pre-population
- **Tests**: Updated existing mocks for new `es_client` parameter signatures; all 196 tests pass

---

## Prometheus Exporter + Grafana Integration (2026-02-14)

- **Feature**: Added Prometheus exporter — reads pre-computed metrics from ES metrics indices and exposes them at `GET /metrics` in Prometheus text format
  - Per-rule metrics: `l2m_rule_{name}_{value_field}` with dimension labels
  - Transform health metrics: `l2m_transform_health`, `l2m_transform_docs_processed`, `l2m_transform_docs_indexed`
  - Scrape-time collection (queries ES on each scrape, no background threads)
  - 60-second scrape interval, 24-hour ES query lookback window
  - Deduplicates by dimension combination (keeps latest value)
  - Clears all gauge label sets between scrapes to prevent stale label combinations from deleted/recreated rules
- **Feature**: Added Grafana with auto-provisioned dashboard
  - Panels: Transform health (stat), docs processed/indexed (stat), per-rule metrics (repeating timeseries)
  - Grafana monitoring link in Rules Manager tab (opens directly to dashboard)
  - Prometheus datasource pre-configured, instance/job labels dropped via metric_relabel
  - Access: http://localhost:3001 (admin/admin)
- **Stack**: Added Prometheus (port 9091) and Grafana (port 3001) to Docker Compose
- **Deps**: Added `prometheus-client==0.21.0`
- **Tests**: Added 29 unit tests for `prometheus_exporter.py` (total: 196)

---

## Inline log generator, remove log-generator containers (2026-02-14)

- Moved log generation logic from the separate `log-generator/` Docker service into `api/log_generator.py`
- API debug endpoints (`/api/debug/generate`, `generate-recent`, `generate-toy`, `DELETE logs`) now call local functions using the resolved ES client instead of proxying to a separate container
- Removed `log-generator` and `log-generator2` services from `docker-compose.yml` (7 services → 5)
- Removed `_get_log_generator_url` and `log_generator` keys from `_KIBANA_SERVICE_MAP`
- Also removed `--reload` flag from API Dockerfile (was causing increasing CPU usage from file watcher)

---

## Original Plan Phases (Specs)

### Phase 1 Spec: Local Dev Environment + Synthetic Logs
- Docker Compose: ES + Kibana (single-node, dev mode)
- On-demand log generator FastAPI service with UI
- Log shape: `timestamp`, `service`, `status_code`, `endpoint`, `response_time_ms`, `tenant`, `level`
- Seed 2-3 Kibana dashboards with varying suitability panels

### Phase 2 Spec: Core Domain Model + REST API (CRUD)
- Python + FastAPI, `LogMetricRule` Pydantic model, SQLite via SQLModel
- Full CRUD: POST/GET/PUT/DELETE on `/api/rules`

### Phase 3 Spec: ES & Kibana Read-Only Connectors
- ES: list_indices, get_mapping, get_field_cardinality, get_index_stats
- Kibana: list_dashboards, get_dashboard, parse_panels → `PanelAnalysis`

### Phase 4 Spec: Suitability Scoring + Candidate Analysis
- 6 scoring signals: date_histogram (+25), numeric aggs (+20), no raw docs (+15), aggregatable dims (+10), lookback (+15), auto-refresh (+10)
- `POST /api/analyze/dashboard/{id}` endpoint

### Phase 5 Spec: Cost Estimation + Guardrails
- Log vs metric storage comparison, series count estimation
- 4 guardrails: dimension_limit, cardinality < 100K, high_cardinality_fields block, net_savings > 0

### Phase 6 Spec: Elastic Metrics Backend
- Abstract `MetricsBackend` interface
- `ElasticMetricsBackend`: ILM → index → transform → start lifecycle
- Status transitions: draft→active = provision, active→draft = deprovision

### Phase 7 Spec: Portal UI
- Originally planned as React + Vite; implemented as enhanced `debug_ui.html` (no separate SPA needed)
- Dashboard list, analysis table, rule creation wizard, rules management

---

## Completed Phases

### Phase 1: Local Dev Environment + Synthetic Logs

- Docker Compose stack with ES 8.12 + Kibana 8.12 + on-demand log generator
- Kibana dashboard "App Service Overview" with 3 panels seeded via NDJSON import API

### Phase 2: Core Domain Model + REST API (CRUD)

- `api/` FastAPI service with `LogMetricRule` CRUD (SQLite via SQLModel)
- Full lifecycle verified: create, list, get, update, delete
- Validation returns 422 with clear errors; data persists across restarts

### Phase 3: ES & Kibana Read-Only Connectors

- `es_connector.py` — list indices, get mappings, field cardinality, index stats
- `kibana_connector.py` — list dashboards, parse panels into structured `PanelAnalysis` objects
- 6 new REST endpoints for ES and Kibana metadata

### Phase 4: Suitability Scoring + Candidate Analysis

- `scoring.py` — deterministic suitability score (0-95) with 6 signals
- `analyzer.py` — dashboard analyzer resolving field types via ES
- Verified scores: "Errors/min by service" → 85, "Avg latency by endpoint" → 85, "Recent log lines" → 20

### Phase 5: Cost Estimation + Guardrails

- `cost_estimator.py` — log vs metric storage cost comparison, query speedup estimation
- `guardrails.py` — 4 pre-creation checks: dimension_limit, cardinality, high_cardinality_fields, net_savings

### Phase 6: Elastic Metrics Backend (Transform Provisioning)

- `elastic_backend.py` — ILM policy → metrics index → continuous transform → start
- Rule lifecycle integration: active triggers provision, delete triggers deprovision
- Handles all 4 compute types: count, sum, avg, distribution (percentiles)

### Phase 7: Portal UI

- Enhanced `debug_ui.html` into a self-service portal with Pipeline + Rules Manager tabs
- Dashboard selector, inline editing, compare, activate/pause, delete
- Refactored `runStep5()` into reusable `runComparison()`

---

## Post-Phase 7: Features & Enhancements

- **Rule origin tracking**: `OriginConfig` model linking rules to source dashboard/panel
- **Multi-Kibana connection**: Session-level URL + auth override from portal UI
- **Metrics dashboard creation**: Visualization cloning, data views, panel add/remove
- **Connection-aware proxy routing**: `_KIBANA_SERVICE_MAP` routes all proxies by connected Kibana
- **Status refresh + shared polling**: `renderStatus()`, `refreshStatus()`, `pollStatus()` shared functions
- **Configurable transform frequency**: Optional `frequency` field on `GroupByConfig`
- **Dev workflow**: Bind-mount `api/` directory + uvicorn `--reload` for live editing
- **Live injection (Step 6)**: New pipeline step to inject recent events after transforms are running, re-run comparison, and watch metric counts update. New `POST /generate-recent` endpoint in log-generator spreads logs across last 30 seconds so transforms pick them up quickly. Extracted shared `_build_log_docs()` helper to avoid duplication between `/generate` and `/generate-recent`.
- **Comparison query fixes**: Fixed three bugs in the side-by-side comparison that caused log-side and metric-side results to diverge: (1) metric query sorted descending while log query sorted ascending — now both sort ascending; (2) metric query size capped at 200, truncating results — increased to 10000; (3) only first dimension used in log aggregation query — now builds nested terms aggs for ALL dimensions.
- **Transform sync delay reduced**: Changed transform `sync.time.delay` from 60s to 1s in `elastic_backend.py` so injected events are picked up faster during demos. Note: delay is baked into transforms at creation time — existing rules must be cleaned up and recreated.
- **Step 6 schedule-now + auto-wait**: After injection, calls ES `_schedule_now` API on each transform to trigger an immediate checkpoint (bypasses the 1-minute frequency wait). Re-run Comparison polls `docs_indexed` until it increases, then runs comparison. Inject → process → compare now takes seconds, not minutes.
- **Generate-recent timestamps at now**: Changed `/generate-recent` from 30s spread to `max_age_seconds=0` (all events at exactly `now`). The initial 24h generation advances the transform checkpoint to ~now, sealing all past buckets. Any spread into past seconds risks landing in an already-closed bucket — so zero spread is the only safe option.
- **Upstream error messages improved**: Fixed global `HTTPStatusError` handler in `api/main.py` — previously all upstream HTTP errors were labeled "Kibana resource not found". Now shows actual upstream URL and status code (e.g., "Upstream resource not found: http://log-generator:8000/generate-recent").

## Production Hardening

- **Configurable `sync.time.delay`**: New `sync_delay` field on `GroupByConfig` (default `"30s"`, was hardcoded `"1s"`). Exposed in both Step 3 panel creation and Rules Manager edit form. The previous 1s default silently dropped late-arriving events. Changing delay on an active rule now triggers automatic deprovision + reprovision (delay is baked into transforms). Config changes to `group_by`, `compute`, or `source` on active rules now correctly reprovision.
- **Server-side transform health monitoring**: Background async task checks all active rules' transform health every 60s (configurable via `HEALTH_CHECK_INTERVAL` env var). If a transform is `red` or `stopped`, the rule's status is automatically set to `error`. New `GET /api/health` endpoint returns monitor state: `monitor_running`, `last_check_time`, `check_interval_seconds`, `rules_in_error`. The monitor catches all exceptions to never crash the main app.
- **Graduated auto-refresh scoring**: Replaced binary 0/10 scoring for the `auto_refresh` signal with a graduated scale: ≤10s→10pts, ≤30s→8, ≤1m→6, ≤5m→4, ≤30m→2, >30m→1, disabled→0. Explanation text now includes the interval and qualitative description (frequent/moderate/infrequent).
- **Clearer UI labels for timing settings**: Renamed "Frequency" → "Check Interval" and "Delay" → "Late Data Buffer" across Step 3, Rules Manager cards, and Rules Manager edit form. Added tooltip and inline help text explaining what each setting does and how to choose values. API field names (`frequency`, `sync_delay`) unchanged for backward compatibility.
- **Bucket auto-fill from panel interval**: The Bucket dropdown in Step 3 now pre-selects the panel's actual `date_histogram` interval (from `fixed_interval`, `calendar_interval`, or legacy `interval` in the Kibana visualization). Panels using Kibana's `auto` interval fall back to `1m`. New `date_histogram_interval` field on `PanelAnalysis` model. Bucket label shows `*` when auto-filled, with a tooltip explaining auto-interval vs fixed bucket tradeoff.
- **Compare side-by-side alignment**: Both sides of the comparison now sort rows identically (by timestamp then dimension string), use the same label format, and cap query/result panels at fixed heights with scroll. Easier to visually verify that pre-computed metrics match raw log aggregations.

---

## Bug Post-Mortems

### Bug 1: `innerHTML +=` Destroying DOM References
- **Symptom**: Rules Manager showed "Checking..." forever for all rules except the last
- **Root cause**: `container.innerHTML += cardHtml` in a loop re-serializes and re-parses the entire container. All previously captured DOM references become detached nodes. Only the last rule survived.
- **Fix**: Build all card HTML as a single string, assign once with `innerHTML =`, then start polling.
- **Why missed**: No DOM/UI tests. Single-rule testing wouldn't reveal it — bug only manifests with 2+ rules.

### Bug 2: Dashboard Selector Not Updating Panels
- **Symptom**: Changing the dashboard dropdown didn't reload Step 3 panels
- **Root cause**: `onDashboardChange()` updated `state.dashboardId` but never called `loadPanels()`.
- **Fix**: Added `loadPanels()` call when Step 3 is already unlocked.
- **Why missed**: No UI interaction tests. Only the initial flow was manually tested.

### Bug 3: Kibana 401 on Security-Enabled Instances
- **Symptom**: Creating a metrics dashboard on kibana2 (security enabled) returned 401
- **Root cause**: `_KIBANA_SERVICE_MAP` had `es_auth` but no `kibana_auth`. Pattern applied asymmetrically.
- **Fix**: Added `kibana_auth` to service map; `get_kibana_conn` auto-fills for known instances.
- **Why missed**: No integration tests against security-enabled stack.

### Bug 4: NDJSON `missing_references` on Add Panel
- **Symptom**: "Add Panel" failed with `missing_references` for some rules
- **Root cause**: `_create_data_view()` silently returned 400 when metrics index didn't exist. Subsequent NDJSON import failed because data view was never created.
- **Fix**: Include data view as `index-pattern` saved object in the NDJSON import batch. References resolve atomically.
- **Why missed**: Only tested with rules whose metrics indices already existed. The swallowed 400 masked the real problem.

### Bug 5: `doc_count` Reserved Field Name (Count = 1)
- **Symptom**: Count values showed 1 instead of actual count (e.g., 6)
- **Root cause**: `doc_count` is an ES reserved field (`_doc_count_field_name`). `sum(doc_count)` returns bucket document count (1), not the stored value.
- **Fix**: Renamed to `event_count` across `elastic_backend.py`, `kibana_connector.py`, `debug_ui.html`.
- **Why missed**: No end-to-end test verifying actual numeric values. Conflict only manifests at aggregation query time.

### Bug 6: Raw `fetch()` Bypassing Connection Headers
- **Symptom**: Delete operations didn't route to correct backend on non-default Kibana
- **Root cause**: `mgrDeleteRule()` and `cleanup()` used raw `fetch()` instead of `api()` wrapper. These call sites predated multi-Kibana and weren't migrated.
- **Fix**: Replaced `fetch()` with `api()`. Added 204 No Content handling.
- **Why missed**: No multi-connection testing. No static analysis enforcing `api()` usage.

### Bug 8: Table/Pie Panel Aggs Silently Dropped (`schema` Mismatch)
- **Symptom**: Table panels ("Top Queried Fields") and pie panels ("Query Type Distribution") parsed with `group_by_fields: []` and empty `es_query.aggs: {}`. Preview Agg showed no results.
- **Root cause**: Kibana's visState uses three `schema` values for bucket aggs: `"segment"` (x-axis in charts), `"group"` (split series), `"bucket"` (table rows). Additionally, `"segment"` is used for both `date_histogram` AND `terms` (pie slices, bar x-axis). Our code only handled `"segment"` as date_histogram and `"group"` as terms — missing `"bucket"` entirely and dropping non-date_histogram `"segment"` aggs.
- **Fix**: `"bucket"` schema routed to group-by. Non-date_histogram `"segment"` schema routed to group-by. Applied to both `_parse_visualization()` (field extraction) and `_build_es_query_from_vis_aggs()` (query building).
- **Why missed**: Initially developed against time-series panels (line charts) which use `"segment"` only for `date_histogram` and `"group"` for terms. Never tested with table or pie visualizations. No unit test included a `schema: "bucket"` or a non-date_histogram `schema: "segment"` agg.
- **Pattern**: Same class of bug as Bug 3 (auth parity) — handling some variants of an external system's enum but not all. The fix is the same: enumerate all variants at development time, not discovery time.

### Bug 9: Preview Agg Missing Time Range Filter
- **Symptom**: Preview Agg aggregated over the entire index history. A table showing "Top 20 fields in last 24h" in Kibana showed different (larger) counts in our preview.
- **Root cause**: Kibana injects a time range filter (`range` on the time field) from the dashboard time picker on every query. Our preview used the panel's stored `es_query` as-is — which doesn't include the time range (Kibana adds it at render time, not save time).
- **Fix**: Preview Agg reads the existing Lookback dropdown value and wraps the query in `bool.filter[range]`.
- **Why missed**: Tested with small synthetic datasets where total count ≈ recent count, so the omission wasn't obvious. Only visible when previewing against a real index with months of data.

### Bug 7: "Checking..." Forever on Zero-Match Transforms
- **Symptom**: Active rules with no matching docs showed "Checking..." for 60s
- **Root cause**: Poll stop condition required `health === 'green' && last_checkpoint`. Zero-match transforms reach green but may not checkpoint quickly.
- **Fix**: Show whatever backend returns immediately. Only poll for `yellow` (transitioning).
- **Why missed**: Only tested with data that matched. Zero-match edge case never considered.

---

## Systemic Test Gaps (at time of bugs)

1. **No UI/DOM tests** — portal is a single HTML file with inline JS. All DOM bugs were invisible.
2. **No end-to-end integration tests** — no test verified actual metric values after transform execution.
3. **No multi-configuration tests** — tests only ran against default no-security stack.
4. **No edge-case coverage** — zero-match filters, missing indices, race conditions untested.
5. **No data correctness assertions** — tests checked HTTP status codes but not response body values.
6. **No static analysis for UI code** — no way to enforce patterns like "use `api()` not `fetch()`".
7. **Swallowed errors** — silent 400 responses masked real failures.

These gaps were partially addressed by the test suite added post-Phase 7 (135 tests across 12 files at that time; now 196 tests across 13 files), including regression tests for all 7 bugs and static analysis anti-pattern checks.

---

## Lessons Learned (Operational)

1. Kibana Lens panels via API require migration-compatible structure. Legacy `visualization` saved objects are more reliable.
2. `searchSourceJSON` is required in dashboard attributes or Kibana crashes.
3. `categoryAxes`/`valueAxes` in `visState.params` need full sub-object structure.
4. Kibana returns 400 (not 409) for duplicate data views.
5. NDJSON import with `overwrite=true` is the most reliable way to seed Kibana objects.
6. Kibana prefixes panel reference names with `{panelIndex}:` but panels store without prefix.
7. `httpx` doesn't follow redirects by default — use `follow_redirects=True`.
8. `SQLModel.create_all` only creates tables, never alters. Use manual `ALTER TABLE`.
9. Bind-mount frequently-edited files for live reload instead of baking into Docker images.
10. `origin.panel_id` stores the dashboard panel index, not the visualization saved object ID.
11. Docker-internal hostnames aren't reachable from the browser — use `_KIBANA_URL_MAP`.
12. `refreshInterval` in NDJSON must be a raw object, not a JSON string.
13. Proxy endpoints must route by connected Kibana via `_KIBANA_SERVICE_MAP`.
14. Raw `fetch()` bypasses connection headers — always use `api()` wrapper.
15. `innerHTML +=` in a loop destroys DOM references.
16. Data view creation via REST API is fragile — prefer NDJSON import with `index-pattern` type.
17. `doc_count` is a reserved ES field name — use `event_count`.
18. ES continuous transforms only process docs FORWARD from their last checkpoint — backdated events are permanently invisible. Inject events with timestamps near `now`.
19. Transform `sync.time.delay` is baked into the transform at creation time. Changing the value in code only affects new rules — existing rules must be deleted and recreated.
