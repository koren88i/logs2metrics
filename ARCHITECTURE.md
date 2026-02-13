# Logs2Metrics PoC - Architecture

## Overview

A platform service that derives metrics from existing Elasticsearch logs, driven by analysis of Kibana dashboards. The system recommends, validates, and provisions metric conversions — reducing storage and query costs without changing application code.

```
+------------------+     +------------------+     +-----------------+
|  Applications    |---->|  Elasticsearch   |<----|  Kibana         |
|  (emit logs)     |     |  (log storage)   |     |  (dashboards)   |
+------------------+     +--------+---------+     +--------+--------+
                                 |                        |
                        read indices/mappings    read dashboards/panels
                                 |                        |
                         +-------v------------------------v-------+
                         |       Logs2Metrics Service              |
                         |  (Control Plane + Analysis + Portal)    |
                         |                                         |
                         |  [Connectors]  [Scoring]  [Guardrails]  |
                         |  [Cost Est.]   [Rule CRUD] [Backend]    |
                         +-------------------+---------------------+
                                             |
                                    provision transforms
                                             |
                                 +-----------v-----------+
                                 |  Metrics Backend      |
                                 |  (ES Transforms +     |
                                 |   Metrics Indices)    |
                                 +-----------------------+
```

---

## Getting Started

```bash
cd logs2metrics
docker compose up -d --build
# Wait for Kibana to be healthy (~30s)
cd seed-dashboards
pip install -r requirements.txt
python seed.py --kibana http://localhost:5602
```

Portal UI: `http://localhost:8091/debug` | Swagger: `http://localhost:8091/docs`

---

## Components

### Docker Compose Stack

| Service | Image / Build | Host Port | Container Port |
|---------|---------------|-----------|----------------|
| Elasticsearch 8.12 | `docker.elastic.co/elasticsearch/elasticsearch:8.12.0` | 9201 | 9200 |
| Kibana 8.12 | `docker.elastic.co/kibana/kibana:8.12.0` | 5602 | 5601 |
| Log Generator | `./log-generator` (FastAPI) | 8090 | 8000 |
| API | `./api` (FastAPI) | 8091 | 8000 |

### Log Generator (`log-generator/`)
- FastAPI service with inline HTML UI
- `POST /generate { count }` — creates a batch of structured logs (random, spread across last 24h)
- `POST /generate-recent { count }` — creates logs with timestamps at exactly `now` (for live injection after transforms are running; all events land in the current open bucket so the transform picks them up)
- `POST /generate-toy` — creates a predictable toy dataset: 10 identical logs (auth-service, /api/login, acme-corp, status 200, 42ms) within a single 1-minute window, for verifiable end-to-end testing
- `DELETE /logs` — deletes all documents from the log index
- `GET /status` — last batch result

### Seed Dashboards (`seed-dashboards/`)
- Python script using Kibana saved objects NDJSON import API
- Idempotent (fixed IDs, `overwrite=true`)

### API Service (`api/`)
- FastAPI + SQLModel + SQLite
- `LogMetricRule` CRUD at `/api/rules`
- ES connector (`es_connector.py`) — index metadata, mappings, cardinality, stats via `elasticsearch-py`
- Kibana connector (`kibana_connector.py`) — dashboard listing, panel parsing, metrics dashboard CRUD + visualization cloning via `httpx`; supports per-request URL + basic auth override via `KibanaConnection` dataclass
- Connector response models (`connector_models.py`) — IndexInfo, IndexMapping, FieldCardinality, IndexStats, DashboardSummary, PanelAnalysis, DashboardDetail
- Database (`database.py`) — SQLite engine + session + auto-migration for new columns
- Config (`config.py`) — ES_URL / KIBANA_URL from environment variables
- Scoring engine (`scoring.py`) — deterministic 0-95 suitability score with 6 weighted signals
- Dashboard analyzer (`analyzer.py`) — orchestrates connectors + scoring, resolves field types
- Cost estimator (`cost_estimator.py`) — compares log vs metric storage cost, estimates query speedup
- Guardrails (`guardrails.py`) — 4 pre-creation checks: dimension limit, cardinality, high-cardinality fields, net savings
- Backend interface (`backend.py`) — abstract `MetricsBackend` ABC + response models (TransformHealth, ProvisionResult, BackendStatus)
- Elastic backend (`elastic_backend.py`) — ES transform provisioning, status, deprovisioning via `elasticsearch-py`
- Portal UI (`debug_ui.html`) — self-service portal with two tabs served at `GET /debug` (see below):
  - **Pipeline tab**: 6-step interactive walkthrough with dynamic dashboard selector
  - **Rules Manager tab**: persistent rule CRUD (view, edit, compare, activate/pause, delete)
- Swagger UI at `/docs`

---

## Data Flow

```
1. ANALYZE (read-only)
   Kibana Dashboard --> parse panels --> score suitability --> show recommendations

2. CREATE RULE (user action)
   Panel recommendation --> pre-filled rule --> guardrail validation --> save rule

3. PROVISION (automated)
   Saved rule --> ES continuous transform --> metrics index + ILM policy

4. VISUALIZE (user action)
   Create metrics dashboard --> add rule panels (cloned from original vis) --> Kibana dashboard reads from metrics indices

5. QUERY (user benefit)
   Dashboard query --> hits metrics index (fast) instead of log index (slow)
```

---

## Project Structure

```
logs2metrics/
  docker-compose.yml          # ES + Kibana + log-generator + api
  README.md                   # GitHub landing page: overview, quick start, API table
  CLAUDE.md                   # Coding standards & quick reference (auto-loaded by Claude Code)
  ARCHITECTURE.md             # This file — technical reference
  CHANGELOG.md                # Project history: completed phases, bug post-mortems
  pytest.ini                  # Test config: testpaths, pythonpath, markers
  requirements-test.txt       # Test deps: pytest, pytest-cov, httpx
  log-generator/
    Dockerfile
    requirements.txt
    main.py                   # FastAPI + inline HTML UI
  seed-dashboards/
    requirements.txt
    seed.py                   # Kibana data view + dashboard seeder
  api/
    Dockerfile
    requirements.txt
    main.py                   # FastAPI CRUD + connector + analysis endpoints
    models.py                 # LogMetricRule SQLModel + Pydantic schemas
    database.py               # SQLite engine + session
    config.py                 # ES_URL, KIBANA_URL from env vars
    connector_models.py       # Pydantic models for connector responses
    es_connector.py           # ES read-only connector (elasticsearch-py)
    kibana_connector.py       # Kibana connector: read dashboards + write metrics dashboards/visualizations (httpx)
    scoring.py                # Suitability scoring engine (0-95)
    analyzer.py               # Dashboard analyzer (scoring orchestrator)
    cost_estimator.py         # Log vs metric storage cost comparison
    guardrails.py             # Pre-creation validation (cardinality, dimensions, savings)
    backend.py                # Abstract MetricsBackend interface + response models
    elastic_backend.py        # ES transform provisioning (ILM, index, transform lifecycle)
    debug_ui.html             # Portal UI: Pipeline (6-step walkthrough) + Rules Manager (served at GET /debug)
    tests/
      conftest.py             # Shared fixtures: factories, mocks, FastAPI TestClient with in-memory SQLite
      test_models.py          # Pydantic model validation tests
      test_scoring.py         # Scoring engine tests (all 6 signals)
      test_cost_estimator.py  # Cost math + series count tests
      test_guardrails.py      # Guardrail check tests
      test_elastic_backend.py # Transform body + field naming (Bug 5) tests
      test_kibana_connector.py # Vis cloning + NDJSON batch (Bug 4) tests
      test_api_rules.py       # CRUD endpoint tests
      test_api_status.py      # Backend status + zero-doc (Bug 7) tests
      test_api_errors.py      # Health + provision failure tests
      test_service_map.py     # Auth parity (Bug 3) + auto-fill tests
      test_static_analysis.py # Anti-pattern checks (Bugs 1, 5, 6)
```

---

## API Endpoints

### Rule CRUD + Backend
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/rules?skip_guardrails=false` | Create rule (guardrails + provision if active; `skip_guardrails=true` bypasses checks) |
| GET | `/api/rules` | List rules |
| GET | `/api/rules/{id}` | Get rule |
| PUT | `/api/rules/{id}` | Update rule (handles status transitions) |
| DELETE | `/api/rules/{id}` | Delete rule (deprovisions if active) |
| GET | `/api/rules/{id}/status` | Backend health + processing stats |

### ES Connector
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/es/indices?pattern=*` | List indices (name, doc count, size) |
| GET | `/api/es/indices/{index}/mapping` | Field names, types, aggregatable |
| GET | `/api/es/indices/{index}/cardinality/{field}` | Approx distinct count |
| GET | `/api/es/indices/{index}/stats` | Doc count, size, query rate |

### Kibana Connector
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/kibana/dashboards` | List dashboards (id, title) |
| GET | `/api/kibana/dashboards/{id}` | Dashboard with parsed PanelAnalysis list |
| GET | `/api/kibana/test-connection` | Test Kibana connectivity, return version + health |

All Kibana endpoints accept optional `X-Kibana-Url`, `X-Kibana-User`, `X-Kibana-Pass` headers to override the default server connection.

### Analysis
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/analyze/dashboard/{id}?lookback=now-7d` | Score all panels (optional lookback override) |

### Cost Estimation + Guardrails
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/estimate` | Cost estimate + guardrail validation for a draft rule |

### Metrics Dashboard
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/metrics-dashboard` | Create empty Kibana metrics dashboard (body: `{title}`) |
| GET | `/api/metrics-dashboard` | Get dashboard info: id, title, panel count, panels (404 if none) |
| POST | `/api/metrics-dashboard/panels/{rule_id}` | Add rule as cloned visualization panel (409 if already added) |
| DELETE | `/api/metrics-dashboard/panels/{rule_id}` | Remove panel from dashboard + delete visualization & data view |
| DELETE | `/api/metrics-dashboard` | Delete entire metrics dashboard + all associated visualizations & data views |

### Server Config
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/config` | Server-side config (default KIBANA_URL) for UI pre-population |

### Portal UI + Proxies
| Method | Path | Description |
|--------|------|-------------|
| GET | `/debug` | Portal UI: Pipeline + Rules Manager |
| POST | `/api/debug/generate` | Proxy to log-generator (routes by `X-Kibana-Url`) |
| POST | `/api/debug/generate-recent` | Proxy to log-generator — events timestamped at `now` (routes by `X-Kibana-Url`) |
| POST | `/api/debug/generate-toy` | Proxy to log-generator toy scenario (routes by `X-Kibana-Url`) |
| DELETE | `/api/debug/logs` | Proxy to log-generator delete endpoint (routes by `X-Kibana-Url`) |
| POST | `/api/es/search` | Thin ES search proxy (routes by `X-Kibana-Url`) |
| GET | `/api/transforms/{id}` | Proxy to ES _transform API (routes by `X-Kibana-Url`) |
| POST | `/api/transforms/{id}/schedule-now` | Trigger immediate transform checkpoint (bypass frequency wait) |

All proxy endpoints accept the `X-Kibana-Url` header and route to the corresponding backend services via `_KIBANA_SERVICE_MAP` in `main.py`. The service map also includes `kibana_auth` for security-enabled instances — `get_kibana_conn` auto-fills Kibana credentials from the map when the user doesn't provide them, mirroring how `_get_es_client` handles ES auth.

---

## Portal UI (`GET /debug`)

A self-contained single-page application at `http://localhost:8091/debug` with two tabs. No external dependencies beyond the running Docker stack.

### Kibana Connection Bar

At the top of the portal, a connection bar allows pointing at any Kibana instance:
- **URL input**: Pre-populated from `GET /api/config` (server's default `KIBANA_URL`) on page load. Enter any Kibana URL (e.g. `http://kibana2:5601`).
- **Auth toggle**: Click "Auth" to reveal username/password fields for HTTP basic auth.
- **Connect button**: Validates connectivity via `GET /api/kibana/test-connection`, shows Kibana version + health status, and loads dashboards on success. Blocks empty URL with an error message.
- **Auto-connect on load**: If the URL field is pre-populated, the portal auto-connects and shows status immediately.
- Connection headers (`X-Kibana-Url`, `X-Kibana-User`, `X-Kibana-Pass`) are injected on every API call based on current field values (stateless, no persistent session).
- **Docker URL mapping**: "View in Kibana" links map Docker-internal URLs to browser-accessible localhost URLs (`http://kibana:5601` → `http://localhost:5602`, `http://kibana2:5601` → `http://localhost:5603`).

### Tab 1: Pipeline (6-Step Walkthrough)

**Dashboard selector** at the top populates from `GET /api/kibana/dashboards`. Replaces hardcoded dashboard ID.

```
Step 1: Generate Logs
  ├── "Reset Logs" — deletes all documents from the log index
  ├── "Generate 200 Logs" — random realistic data across 24h (additive, does not clear first)
  └── "Toy Scenario (10 identical logs)" — predictable dataset for verification (additive)
       10x (auth-service, /api/login, 200, 42ms) in one 1-minute window
       → expect 1 metric point with count=10

Step 2: See Raw Logs
  └── Fetches latest 10 docs from ES, displays in table
       Unlocks Step 3 and auto-loads dashboard panels

Step 3: Analyze Panels + Create Rules
  ├── Per-panel card showing: type, index, aggs, dimensions, metrics
  └── Per-panel actions:
       ├── "Preview Agg" — runs the panel's ES aggregation inline (date_histogram + terms + metric)
       │    Shows query, matching docs, buckets with data, results table
       ├── Lookback selector (1h / 6h / 1d / 7d / 30d / 90d / 1y) — for Analyze scoring
       ├── "Analyze" — runs suitability scoring (0-95) with per-signal breakdown bars
       ├── Bucket selector (10s / 1m / 5m / 10m / 1h) — metric aggregation granularity
       ├── Frequency selector (auto / 1m / 5m / 15m / 1h) — how often transform checks for new data
       ├── Skip guardrails checkbox
       └── "Create Rule" — auto-constructs RuleCreate from panel, POSTs to /api/rules
            Infers compute type from panel metrics (count/avg/sum/distribution)
            Maps group-by fields and filter queries

Step 4: Created Rules + Transforms
  └── Shows provisioned rules with live polling:
       ├── Transform ID + metrics index name
       ├── Rule body JSON
       ├── Health status (polls every 2s until green + checkpointed) + Refresh button
       └── Transform definition + stats on ready

Step 5: Side-by-Side Comparison
  └── For each rule, runs two queries in parallel:
       ├── LEFT: Log aggregation query against source index
       │    (date_histogram + terms + metric agg, matching rule's filter/bucket/dims)
       └── RIGHT: Simple fetch from pre-computed metrics index
       Shows: query JSON, results table, docs scanned vs metric docs, query times, reduction %
       Column headers adapt to compute type: Count / Avg(field) / Sum(field) / Pct(field)

Step 6: Live Injection
  └── After initial comparison, inject more data and watch transforms update:
       ├── "Inject 50 Recent Events" — generates logs with timestamps at `now`,
       │    then calls _schedule_now on each transform to trigger immediate processing
       ├── "Re-run Comparison" — re-executes side-by-side comparison directly.
       │    Shows updated doc counts and metric values
       └── "Cleanup" — deletes all session rules + transforms + metrics indices
```

### Tab 2: Rules Manager

Persistent rule management across sessions. Auto-loads on tab switch via `GET /api/rules`.

```
Per-rule card:
  ├── Rule metadata: name, status badge, compute type, dimensions, time bucket
  ├── Source info: index pattern, filter, time field
  ├── Transform info: ID, metrics index name
  ├── Origin: "From: Dashboard Title > Panel Title" with clickable Kibana link
  ├── Live status: health, docs processed/indexed (single fetch; polls only for yellow/transitioning)
  └── Actions (by status):
       ├── active:  [Edit] [Compare ▼] [Add Panel] [Pause] [Delete]
       ├── draft:   [Edit] [Activate] [Delete]
       ├── paused:  [Edit] [Activate] [Delete]
       └── error:   [Edit] [Activate] [Delete]

Compare: expands inline with side-by-side log agg vs metrics (reuses runComparison)
Edit: inline form (name, time bucket, dimensions, compute); deprovisions→saves→re-provisions active rules
Add Panel: adds rule's cloned visualization to the metrics dashboard (shown only for active rules; disabled after adding)
Remove Panel: removes rule's panel from the metrics dashboard + deletes visualization & data view saved objects
Activate/Pause: status transitions via PUT /api/rules/{id}
Delete: confirms, then DELETE /api/rules/{id} (API handles deprovision)

Metrics Dashboard section (above rule list):
  ├── If no dashboard: name input + "Create Dashboard" button
  └── If dashboard exists: title, panel count, "View in Kibana" link, "Delete Dashboard" button
       Delete Dashboard: removes entire metrics dashboard + all associated visualizations & data views (handles orphaned panels)
```

### Key Behaviors

- **Kibana connection**: Session-level URL override with optional basic auth. URL pre-populated from server config on load, auto-connects. All API calls inject connection headers based on current field values (stateless).
- **Dashboard selector**: Dynamically populated; drives panel loading and analysis across the Pipeline tab. Changing the selection reloads Step 3 panels immediately if the step is already unlocked.
- **Guardrail bypass**: Small test datasets often fail the net_savings guardrail (metric storage > log storage). The "Skip guardrails" checkbox passes `?skip_guardrails=true` to the API.
- **Status display (Rules Manager)**: Shows whatever the backend returns immediately (green/red/unknown/stopped with docs processed). Only polls further if health is `yellow` (transitioning). Does NOT require a checkpoint — avoids the infinite "Checking..." bug with zero-match transforms.
- **Zero-match handling (Pipeline tab)**: Transforms that match zero documents (e.g. error filter with all-200 data) are considered ready once they reach `health: green` with a completed checkpoint, regardless of `docs_processed`.
- **Cleanup**: Button at the end of Pipeline deletes all session rules + transforms + metrics indices and resets the UI.
- **Rules Manager persistence**: Shows all rules from the database regardless of which session created them.
- **Metrics Dashboard**: One dashboard at a time (fixed ID `l2m-metrics-dashboard`). Panels are cloned from original visualizations — preserves chart type, axes, legend, colors, date_histogram + terms aggs. Only the metric agg is rewired to read pre-computed fields (`event_count`, `sum_{field}`, `avg_{field}`, `pct_{field}`) from `l2m-metrics-rule-{id}` indices. Each rule gets its own Kibana data view. Panels can be removed individually or the entire dashboard can be deleted (cleans up all associated saved objects).

---

## Key Design Principles

1. **Read-only analysis** — never modify existing Kibana dashboards
2. **Explicit opt-in** — user approves every metric conversion
3. **Cost guardrails** — block conversions that increase cost
4. **Backend-agnostic** — abstract interface, ES transforms for PoC, Prometheus/Thanos later
5. **No app code changes** — works entirely from existing log data
6. **API-first** — all functionality exposed via REST before UI

---

## Domain Model

`LogMetricRule` — persisted in SQLite, exposed via REST.

```
LogMetricRule
  id: int (auto)
  name: string
  owner: string
  source:
    index_pattern: string       # e.g. "app-logs*"
    time_field: string          # default "timestamp"
    filter_query: dict?         # optional ES query DSL
  group_by:
    time_bucket: string         # e.g. "1m", "5m"
    dimensions: string[]        # e.g. ["service", "endpoint"]
    frequency: string?          # transform check interval, e.g. "1m", "5m", "15m"; defaults to max(time_bucket, 1m)
  compute:
    type: count|sum|avg|distribution
    field: string?              # required for sum/avg/distribution
    percentiles: float[]?       # e.g. [50, 90, 95, 99]
  backend_config:
    type: elastic               # future: prometheus
    retention_days: int         # default 450
  origin:
    dashboard_id: string        # Kibana dashboard ID (e.g. "l2m-app-overview")
    dashboard_title: string     # Human-readable dashboard name
    panel_id: string            # Panel ID within the dashboard
    panel_title: string         # Human-readable panel name
  status: draft|active|paused|error
  created_at: datetime
  updated_at: datetime
```

---

## Key Artifact IDs

| Artifact | Value |
|----------|-------|
| ES index | `app-logs` |
| Data view ID | `l2m-app-logs` |
| Dashboard ID | `l2m-app-overview` |
| Saved search ID | `l2m-recent-logs` |
| Vis: errors | `l2m-errors-by-service` |
| Vis: latency | `l2m-latency-by-endpoint` |
| Metrics dashboard | `l2m-metrics-dashboard` |
| Metrics vis (per rule) | `l2m-metrics-vis-rule-{id}` |
| Metrics data view (per rule) | `l2m-metrics-dv-rule-{id}` |

---

## ES Index Schema (`app-logs`)

```json
{
  "timestamp": "date",
  "service": "keyword",
  "endpoint": "keyword",
  "status_code": "integer",
  "response_time_ms": "float",
  "tenant": "keyword",
  "level": "keyword",
  "message": "text"
}
```

---

## Seeded Dashboard Panels

| Panel | Type | Metric Suitability |
|-------|------|--------------------|
| Errors/min by service | line (date_histogram + terms + count, filter: status_code >= 500) | HIGH |
| Avg latency by endpoint | line (date_histogram + terms + avg on response_time_ms) | HIGH |
| Recent log lines | saved search (raw docs table) | LOW |
