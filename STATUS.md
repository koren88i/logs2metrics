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
  - **Visualization cloning**: Fetches the original Kibana visualization saved object (from the rule's `origin.panel_id`), deep-copies its structure (chart type, axes, legend, colors, date_histogram + terms aggs), and rewires the metric agg to read pre-computed fields (`doc_count`, `sum_{field}`, `avg_{field}`, `pct_{field}`)
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

---

## All Phases Complete

The full pipeline is operational: analyze Kibana dashboards → score panels → create metric rules → provision ES transforms → compare log queries vs pre-computed metrics → create a Kibana metrics dashboard with cloned visualizations reading from pre-computed data. Manage rules persistently via the Rules tab. Connect to any Kibana instance (with optional auth) from the portal UI. All portal actions (generate, reset, search, compare, delete) route to the correct backend based on the connected Kibana instance.
