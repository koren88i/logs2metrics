# Logs2Metrics PoC - Status & Session Context

> Handoff document for new conversations.
> Read this + PLAN.md + ARCHITECTURE.md to continue implementation.

---

## Current Phase: Post-Phase 7 — Metrics Dashboard & UX Improvements

---

## Completed Phases

### Phase 1: Local Dev Environment + Synthetic Logs [DONE]

- Docker Compose stack with ES 8.12 + Kibana 8.12 + on-demand log generator
- Kibana dashboard "App Service Overview" with 3 panels seeded via NDJSON import API
- All test criteria verified (see PLAN.md)

### Phase 2: Core Domain Model + REST API (CRUD) [DONE]

- `api/` FastAPI service with `LogMetricRule` CRUD (SQLite via SQLModel)
- Full lifecycle verified: create, list, get, update, delete
- Validation returns 422 with clear errors; data persists across restarts
- Swagger UI at `http://localhost:8091/docs`
- All test criteria verified (see PLAN.md)

### Phase 3: ES & Kibana Read-Only Connectors [DONE]

- `api/es_connector.py` — list indices, get mappings, field cardinality, index stats (via `elasticsearch-py`)
- `api/kibana_connector.py` — list dashboards, parse panels into structured `PanelAnalysis` objects (via `httpx`)
- `api/connector_models.py` — Pydantic response models (IndexInfo, IndexMapping, FieldCardinality, IndexStats, DashboardSummary, PanelAnalysis, DashboardDetail)
- `api/config.py` — ES_URL / KIBANA_URL from environment variables
- 6 new REST endpoints: `/api/es/indices`, `/api/es/indices/{index}/mapping`, `/api/es/indices/{index}/cardinality/{field}`, `/api/es/indices/{index}/stats`, `/api/kibana/dashboards`, `/api/kibana/dashboards/{id}`
- API service now depends on ES + Kibana health in docker-compose
- All test criteria verified (see PLAN.md)

---

## How to Start the Stack

```bash
cd logs2metrics
docker compose up -d --build
# Wait for Kibana to be healthy (~30s)
cd seed-dashboards
pip install -r requirements.txt
python seed.py --kibana http://localhost:5602
```

---

## Gotchas & Lessons Learned

1. **Kibana Lens panels via API**: Inline Lens `embeddableConfig.attributes` requires very specific migration-compatible structure. Legacy `visualization` saved objects with `visState` + `aggs` are much more reliable to create programmatically.
2. **Kibana `searchSourceJSON`**: Dashboard attributes MUST include `kibanaSavedObjectMeta.searchSourceJSON` or the dashboard crashes with "Cannot read properties of undefined".
3. **Kibana axis migration**: The `categoryAxes` and `valueAxes` in `visState.params` need full structure including `style`, `scale`, `labels`, `title` sub-objects or the migrator crashes.
4. **Data view duplicate detection**: Kibana returns 400 (not 409) for duplicate data views.
5. **NDJSON import API**: `POST /api/saved_objects/_import?overwrite=true` with multipart file upload is the most reliable way to seed Kibana objects.
6. **Kibana saved object reference names**: Kibana prefixes dashboard panel reference names with `{panelIndex}:` (e.g. `p1:panel_p1`), but panels store `panelRefName` without the prefix (e.g. `panel_p1`). Must check both formats when resolving references.
7. **httpx follow_redirects**: `httpx` does not follow redirects by default (unlike `requests`). Kibana saved objects API may redirect on individual GET-by-ID requests. Use `httpx.Client(follow_redirects=True)`.
8. **SQLModel create_all won't add columns**: `SQLModel.metadata.create_all()` only creates new tables, never alters existing ones. Adding a column to a model requires a manual `ALTER TABLE` migration in `database.py:_migrate()`.
9. **Docker COPY vs bind mount for dev**: Files baked into images via `COPY . .` require a rebuild to update. Bind-mount frequently-edited files (like `debug_ui.html`) in `docker-compose.yml` for live reload.
10. **`origin.panel_id` is NOT a visualization ID**: It stores the dashboard panel index (e.g. `"p1"`), not the visualization saved object ID (e.g. `"l2m-errors-by-service"`). To get the visualization ID, fetch the origin dashboard, find the panel by `panelIndex`, then resolve through the dashboard's `references` array. See `_resolve_panel_vis_id()` in `kibana_connector.py`.
11. **Docker networking in portal UI**: The API container can't reach `localhost:56xx` (host ports). Kibana URLs entered in the portal must use Docker-internal hostnames (`http://kibana:5601`, `http://kibana2:5601`). The `_KIBANA_URL_MAP` in `debug_ui.html` maps these to browser-accessible localhost URLs for "View in Kibana" links.
12. **Kibana `refreshInterval` format**: In NDJSON import, `dashboard.refreshInterval` must be a raw object (`{"pause": false, "value": 30000}`), not a JSON-serialized string. Kibana's object mapping rejects strings for this field.
13. **Proxy endpoints must route by connection**: Portal proxy endpoints (generate, reset, ES search, transforms) were originally hardcoded to the default log-generator/ES. When the user connects to a different Kibana, these must route to the corresponding services. Use `_KIBANA_SERVICE_MAP` in `main.py` to map Kibana URL → log-generator URL + ES URL + auth. The portal's `api()` JS function already injects `X-Kibana-Url` on every call.
14. **Raw `fetch()` bypasses connection headers**: Any JS code using `fetch()` directly instead of the `api()` wrapper won't include `X-Kibana-Url`/auth headers. Always use `api()` for backend calls. The `api()` helper handles 204 No Content responses (returns `null`).
15. **`innerHTML +=` in a loop destroys DOM references**: Building a list of cards with `container.innerHTML += card` inside a loop serializes/re-parses the entire DOM on each iteration, invalidating any previously captured element references (e.g. from `getElementById`). Always build the full HTML string first, then assign once with `container.innerHTML = allCards`, and only then interact with child elements.
16. **Kibana data view creation via REST API is fragile**: `POST /api/data_views/data_view` silently returns 400 when the underlying ES index doesn't exist, and `allowNoIndex: true` doesn't always help. The NDJSON import API then fails with `missing_references` because the data view was never created. Fix: include the data view as an `index-pattern` type saved object directly in the NDJSON import batch — references are resolved within the batch atomically.
17. **`doc_count` is a reserved ES field name**: Elasticsearch treats a field named `doc_count` as `_doc_count_field_name` metadata — it controls the document count returned by aggregations rather than being a regular field. A `sum(doc_count)` aggregation returns the number of documents in the bucket (1), not the stored value (e.g. 6). Always use a different name like `event_count` for pre-computed count fields.

### Phase 4: Suitability Scoring + Candidate Analysis [DONE]

- `api/scoring.py` — deterministic suitability score (0-95) with 6 signals and human-readable breakdown
  - Structural: date_histogram (+25), numeric aggs (+20), no raw docs (+15), aggregatable dimensions (+10)
  - Behavioral: lookback window (+15), auto-refresh (+10)
- `api/analyzer.py` — dashboard analyzer that resolves field types via ES, extracts dashboard behavioral metadata, and scores each panel
- `api/kibana_connector.py` — added `get_data_view_index_pattern()` to resolve data view IDs to ES index patterns
- 1 new REST endpoint: `POST /api/analyze/dashboard/{id}`
- Verified scores: "Errors/min by service" → 85, "Avg latency by endpoint" → 85, "Recent log lines" → 20
- All test criteria verified (see PLAN.md)

### Phase 5: Cost Estimation + Guardrails [DONE]

- `api/cost_estimator.py` — estimates log vs metric storage costs, net savings, query speedup
  - Fetches live index stats & field cardinalities from ES
  - Computes series count as product of dimension cardinalities
  - Compares log storage (docs/day × avg_doc_size × log_retention) vs metric storage (series × points/day × 40 bytes × metric_retention)
- `api/guardrails.py` — 4 pre-creation checks with actionable explanations:
  - `dimension_limit`: max 5 group-by dimensions
  - `cardinality`: estimated series count < 100K
  - `high_cardinality_fields`: blocks known unbounded fields (request_id, session_id, user_id, etc.)
  - `net_savings`: metric storage must be less than log storage
- 1 new REST endpoint: `POST /api/estimate` — returns cost estimate + guardrail results
- `POST /api/rules` — now validates guardrails before accepting; returns 422 with failures + suggested fixes
- All test criteria verified (see PLAN.md)

### Phase 6: Elastic Metrics Backend (Transform Provisioning) [DONE]

- `api/backend.py` — abstract `MetricsBackend` interface with `validate()`, `provision()`, `get_status()`, `deprovision()`
  - Response models: `TransformHealth`, `ValidationResult`, `ProvisionResult`, `BackendStatus`
- `api/elastic_backend.py` — `ElasticMetricsBackend` implementation:
  - `provision(rule)`: creates ILM policy → metrics index → continuous transform → starts transform
  - `get_status(rule_id)`: returns transform health, docs processed/indexed, last checkpoint
  - `deprovision(rule_id)`: stops + deletes transform + deletes metrics index (idempotent)
  - Naming: transforms `l2m-rule-{id}`, indices `l2m-metrics-rule-{id}`, ILM `l2m-metrics-{days}d`
  - Handles all 4 compute types: count, sum, avg, distribution (percentiles)
  - Partial cleanup on provisioning failure
- Rule lifecycle integration in `api/main.py`:
  - `POST /api/rules` with `status: active` triggers provisioning; failure sets status to `error`
  - `PUT /api/rules/{id}` handles status transitions (draft→active = provision, active→draft = deprovision)
  - `DELETE /api/rules/{id}` deprovisions before deleting
  - 1 new endpoint: `GET /api/rules/{id}/status` — returns backend health + processing stats

### Debug UI + Configurable Lookback [DONE]

- `api/debug_ui.html` — 5-step interactive walkthrough at `GET /debug` (http://localhost:8091/debug)
- `POST /api/analyze/dashboard/{id}?lookback=now-7d` — configurable lookback window override
- Proxy endpoints for debug UI (avoid CORS)
- `log-generator/main.py` — added `DELETE /logs` endpoint + delete button in UI

### Phase 7: Portal UI [DONE]

- Enhanced `api/debug_ui.html` into a self-service portal (no separate React SPA needed)
- **Tab navigation**: Pipeline tab (existing 5-step walkthrough) + Rules Manager tab
- **Dashboard selector**: Dynamic dropdown populated from `GET /api/kibana/dashboards`, replaces hardcoded dashboard ID
- **Rules Manager tab** — persistent rule management across sessions:
  - Loads all rules from `GET /api/rules` with live transform status polling
  - **Compare**: Side-by-side log aggregation vs pre-computed metrics (reuses extracted `runComparison()`)
  - **Edit**: Inline form for name, time bucket, dimensions, compute type/field; auto-deprovisions and re-provisions active rules
  - **Activate/Pause**: Status transitions via `PUT /api/rules/{id}`
  - **Delete**: Removes rule + transform + metrics index with confirmation
- Refactored `runStep5()` into reusable `runComparison(ruleInfo, outputEl)` shared by Pipeline and Rules tabs

### Post-Phase 7: Bug Fixes & Enhancements [DONE]

- **Fixed Rules Manager "Checking..." forever bug**: Active rules with no matching docs (e.g. error filter with all-200 data) would spin "Checking..." for 60s because the poll required `health === 'green' && last_checkpoint`. Replaced with single-fetch-and-display: shows whatever the backend returns immediately, only continues polling for `yellow` (transitioning) state.
- **Rule origin tracking**: Each rule now records which Kibana dashboard and panel it was created from.
  - `OriginConfig` model: `dashboard_id`, `dashboard_title`, `panel_id`, `panel_title`
  - Stored as JSON column in SQLite (`origin` on `LogMetricRule`)
  - Rules Manager displays "From: [Dashboard Title](kibana-link) > Panel Title" with clickable Kibana link
  - Existing rules without origin gracefully show nothing
- **Dev workflow: live HTML editing**: `debug_ui.html` is now bind-mounted into the API container (`docker-compose.yml`), so edits take effect on refresh without rebuilding
- **DB migration support**: `database.py` now runs `_migrate()` on startup to add new columns (e.g. `origin`) to existing SQLite tables

### Multi-Kibana Connection Support [DONE]

- **Configurable Kibana URL from the portal UI**: Users can point at any Kibana instance without restarting the stack
  - Connection bar in the portal header: URL input + Auth toggle (username/password) + Test button + status indicator
  - Leave URL empty to use the server's default `KIBANA_URL` env var (fully backward compatible)
  - `KibanaConnection` dataclass in `kibana_connector.py`: encapsulates URL + optional HTTP basic auth credentials
  - All kibana_connector functions accept optional `conn: KibanaConnection` parameter; defaults to the env var when `None`
  - `analyzer.py` threads `conn` through to all kibana_connector calls
  - API endpoints accept `X-Kibana-Url`, `X-Kibana-User`, `X-Kibana-Pass` request headers (extracted via FastAPI `Header` dependency)
  - New endpoint: `GET /api/kibana/test-connection` — probes Kibana `/api/status`, returns version + health
  - Portal `api()` JS function injects connection headers on every fetch when a URL is entered
  - Rules Manager Kibana origin links now use the entered URL (falls back to `http://localhost:5602`)

### Metrics Dashboard Creation [DONE]

- **Create Metrics Dashboard from Rules**: Two-step flow in the Rules Manager tab to visualize pre-computed metrics in Kibana
  - "Create Metrics Dashboard" section at top of Rules Manager — enter a name, click Create to provision an empty Kibana dashboard
  - Per-rule "Add Panel" button on active rules — clones the original visualization and rewires it to read from the metrics index
  - **Visualization cloning**: Fetches the original Kibana visualization saved object (from the rule's `origin.panel_id`), deep-copies its structure (chart type, axes, legend, colors, date_histogram + terms aggs), and rewires the metric agg to read pre-computed fields (`event_count`, `sum_{field}`, `avg_{field}`, `pct_{field}`)
  - Creates per-rule Kibana data views pointing at `l2m-metrics-rule-{id}` indices
  - Fixed dashboard ID (`l2m-metrics-dashboard`) for one-at-a-time semantics
  - "View in Kibana" link with Docker-internal → browser URL mapping (`http://kibana:5601` → `http://localhost:5602`)
  - 3 new API endpoints: `POST /api/metrics-dashboard`, `GET /api/metrics-dashboard`, `POST /api/metrics-dashboard/panels/{rule_id}`
  - New kibana_connector write functions: `create_metrics_dashboard()`, `get_metrics_dashboard()`, `add_rule_panel_to_dashboard()`, `_clone_and_rewire_visualization()`, `_resolve_panel_vis_id()`, `_import_saved_objects()`, `_create_data_view()`, `_fetch_visualization()`

### UX Improvements [DONE]

- **Kibana URL pre-population**: `GET /api/config` returns the server's default `KIBANA_URL`; portal pre-populates the URL field on page load and auto-connects
- **"Connect" button** (renamed from "Test"): Validates connectivity + loads dashboards on success. Empty URL field is blocked with an explicit error message instead of silently using server default
- **Docker URL mapping**: Portal maps Docker-internal Kibana URLs to browser-accessible localhost URLs for "View in Kibana" links (`_KIBANA_URL_MAP`)

### Connection-Aware Proxy Routing [DONE]

- **Separated Step 1 buttons**: "Reset & Generate 200 Logs" split into independent "Reset Logs" (danger), "Generate 200 Logs" (primary), and "Toy Scenario" (secondary). Reset and generate are now independent actions — can generate multiple batches without clearing, or clear without regenerating.
- **Proxy endpoints route to the connected stack**: All portal proxy endpoints now read the `X-Kibana-Url` header and route to the correct backend services:
  - `_KIBANA_SERVICE_MAP` in `main.py` maps each Kibana URL to its corresponding log-generator URL and ES URL (with optional auth)
  - `_get_log_generator_url()`: resolves Kibana URL → log-generator service (e.g. `kibana2:5601` → `log-generator2:8000`)
  - `_get_es_client()`: resolves Kibana URL → ES client with correct URL and auth (e.g. `kibana2:5601` → `elasticsearch2:9200` with basic auth)
  - Affected endpoints: `POST /api/debug/generate`, `POST /api/debug/generate-toy`, `DELETE /api/debug/logs`, `POST /api/es/search`, `GET /api/transforms/{id}`
- **Fixed raw `fetch()` calls bypassing connection headers**: `mgrDeleteRule()` and `cleanup()` used raw `fetch()` instead of `api()`, so DELETE requests didn't include Kibana connection headers. Switched to `api()` and added 204 No Content handling to the `api()` helper.

### Bug Fixes [DONE]

- **Fixed Rules Manager status display for all but last rule**: `container.innerHTML +=` inside the rule rendering loop destroyed and recreated all previous DOM elements on each iteration. `mgrPollStatus()` grabbed a reference to `mgrStatus{id}`, then the next `innerHTML +=` nuked it — status responses wrote to detached nodes. Only the last rule survived. Fix: build all card HTML first, assign once with a single `innerHTML =`, then start polling.
- **Fixed dashboard selector not updating Step 3 panels**: `onDashboardChange()` only updated `state.dashboardId` but never called `loadPanels()`. Changing the dashboard dropdown now reloads panels in Step 3 if already unlocked.
- **Fixed Kibana 401 on metrics dashboard creation**: `_KIBANA_SERVICE_MAP` had `es_auth` for kibana2 but no `kibana_auth`. When users connected to a security-enabled Kibana without entering credentials, write operations (saved_objects import) returned 401. Fix: added `kibana_auth` to the service map and auto-fill credentials in `get_kibana_conn` for known Kibana instances (mirrors how `_get_es_client` already works for ES auth).

### Status Refresh + Shared Polling [DONE]

- **Refresh button on all status displays**: Both Pipeline Step 4 and Rules Manager now show a "Refresh" button next to health/processed/indexed stats. Clicking it fetches live transform stats from ES on demand — numbers update when new data arrives and the transform processes it.
- **Shared status functions**: Refactored duplicated status logic into three shared functions used everywhere:
  - `renderStatus(statusEl, st, ruleId)` — renders health/processed/indexed + refresh button
  - `refreshStatus(ruleId)` — fetches live stats and re-renders (auto-finds the right DOM element for Pipeline or Rules Manager)
  - `pollStatus(ruleId, statusElId, opts)` — configurable polling loop with stop condition, callbacks, and cleanup handle storage
- Previously, status numbers were a one-time snapshot taken shortly after rule creation and never updated.

### Configurable Transform Frequency [DONE]

- **`frequency` field on `GroupByConfig`**: Optional transform check interval (e.g. `1m`, `5m`, `15m`, `1h`). Defaults to `null` which means auto = `max(time_bucket, 1m)`.
- **Pipeline Step 3**: New "Frequency" dropdown next to "Bucket" dropdown, with options: auto, 1m, 5m, 15m, 1h
- **Rules Manager**: Frequency shown in card metadata, editable in the inline edit form
- **Backend**: `elastic_backend.py` uses explicit frequency if set, otherwise falls back to auto logic
- **Labeled dropdowns**: All three Step 3 dropdowns now have labels: "Lookback", "Bucket", "Frequency"

### Data View NDJSON Fix [DONE]

- **Fixed "Add Panel" failing for some rules**: `_create_data_view()` via REST API silently failed (400 swallowed) when the metrics index didn't exist yet, causing the subsequent NDJSON import to fail with `missing_references`. Fix: include the data view as an `index-pattern` saved object directly in the NDJSON import batch alongside the visualization and dashboard. The import resolves references within the batch atomically.

### Dev Workflow: Full Bind-Mount + Auto-Reload [DONE]

- **Bind-mount entire `api/` directory**: `docker-compose.yml` now mounts `./api:/app` instead of just `debug_ui.html`. All Python files, HTML, everything updates live without rebuilding.
- **Uvicorn `--reload`**: Dockerfile CMD now includes `--reload` so uvicorn watches for file changes and auto-restarts the server.
- After one final `docker compose up -d --build api`, no more rebuilds needed for any `api/` code changes.

### Count Mismatch Fix + Metrics Dashboard Management [DONE]

- **Fixed count mismatch between original and metrics dashboards**: The count transform aggregation field was named `doc_count`, which is a reserved field name in Elasticsearch (`_doc_count_field_name` metadata). Kibana's `sum(doc_count)` returned 1 (number of metrics documents) instead of the actual stored count (e.g. 6). Renamed to `event_count` across 4 files: `elastic_backend.py` (transform agg + index mapping), `kibana_connector.py` (`_METRIC_AGG_MAP` + fallback), `debug_ui.html` (comparison metric field).
- **Panel removal from metrics dashboard**: New `DELETE /api/metrics-dashboard/panels/{rule_id}` endpoint + `remove_rule_panel_from_dashboard()` in `kibana_connector.py`. Removes the panel from the dashboard's `panelsJSON`, deletes the visualization and data view saved objects. "Remove Panel" button added to active rule cards in the Rules Manager.
- **Metrics dashboard deletion**: New `DELETE /api/metrics-dashboard` endpoint that nukes the entire metrics dashboard and all associated visualizations and data views. "Delete Dashboard" button in the Metrics Dashboard section of the Rules Manager. Handles orphaned panels (rules deleted before panels were removed).
- **`_delete_saved_object()` helper** in `kibana_connector.py`: Generic DELETE for Kibana saved objects by type and ID. Used by both panel removal and dashboard deletion.

### Test Suite [DONE]

- **135 unit/integration tests across 12 files** — all passing, no Docker required
- Created from scratch: `pytest.ini`, `requirements-test.txt`, `api/tests/` with `conftest.py` + 11 test files
- **Regression tests for all 7 post-Phase-7 bugs**: innerHTML += (Bug 1), auth parity (Bug 3), NDJSON batch (Bug 4), reserved field names (Bug 5), raw fetch (Bug 6), zero-match status (Bug 7)
- Covers: Pydantic model validation, scoring engine, cost estimator, guardrails, elastic backend (transform body + field naming), Kibana connector (vis cloning + NDJSON format), FastAPI CRUD endpoints, backend status, service map auth parity, static analysis anti-pattern checks
- **Mocking strategy**: In-memory SQLite with StaticPool, patched ES client/connector, mocked metrics backend, FastAPI TestClient with dependency overrides
- **Fixed Bug 1 regression in `loadPanels()`**: `container.innerHTML +=` inside `forEach` loop (same anti-pattern as the Rules Manager bug) — replaced with string accumulation + single `innerHTML =` assignment
- Run: `python -m pytest -v` from repo root
- See `CLAUDE.md` for test file listing and bug-to-test mapping

---

## All Phases Complete

The full pipeline is operational: analyze Kibana dashboards → score panels → create metric rules → provision ES transforms → compare log queries vs pre-computed metrics → create a Kibana metrics dashboard with cloned visualizations reading from pre-computed data. Manage rules persistently via the Rules tab. Connect to any Kibana instance (with optional auth) from the portal UI. All portal actions (generate, reset, search, compare, delete) route to the correct backend based on the connected Kibana instance.
