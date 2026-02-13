# Logs2Metrics — Project Changelog

> Historical record of completed phases, bug post-mortems, and lessons learned.
> For active coding standards see [CLAUDE.md](CLAUDE.md). For architecture see [ARCHITECTURE.md](ARCHITECTURE.md).

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

These gaps were partially addressed by the test suite (135 tests across 12 files) added post-Phase 7, including regression tests for all 7 bugs and static analysis anti-pattern checks.

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
