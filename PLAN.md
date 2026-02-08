# Logs2Metrics PoC - Phased Implementation Plan

## Context

Application teams emit logs to Elasticsearch and build Kibana dashboards that repeatedly aggregate those logs (count, avg, percentiles by dimensions). This causes high storage cost, high query cost, and poor dashboard performance at scale.

This plan implements a **Logs2Metrics portal** that analyzes Kibana dashboards, recommends metric conversions, enforces cost guardrails, and provisions Elasticsearch transforms to materialize metrics — all without changing application code.

**Key learnings from Datadog's Logs-to-Metrics service incorporated:**
- Simple rule model: `filter + compute_type + group_by dimensions` (Datadog uses exactly this)
- Cardinality guardrails before rule creation (DD warns against unbounded dimensions like user_id, request_id)
- Distribution metrics with optional percentiles (p50/p75/p90/p95/p99)
- API-first design with full CRUD (Datadog exposes a Logs Metrics REST API)
- Cost-awareness baked into the creation flow (DD bills log-based metrics as custom metrics)
- 10s granularity buckets, 15-month metric retention vs short log retention

---

## Phase 1: Local Dev Environment + Synthetic Logs [COMPLETED]

**Goal:** A running local stack with realistic log data and Kibana dashboards to develop against.

**Deliverables:**
1. `docker-compose.yml` — Elasticsearch + Kibana (single-node, dev mode)
2. **On-demand log generator** — a small FastAPI service (Docker service) with a simple UI:
   - **UI controls:**
     - Batch size input (e.g. 500, 5000, 50000 logs)
     - "Send Batch" button — generates and ingests exactly that many logs
     - Status display — shows last batch result (count sent, duration, index name)
   - **Log shape:** structured JSON simulating a web service:
     - Fields: `timestamp`, `service`, `status_code`, `endpoint`, `response_time_ms`, `tenant`, `level`
   - **API endpoints** (UI calls these):
     - `POST /generate` `{ "count": 5000 }` — send a batch
     - `GET /status` — last batch info
3. Seed Kibana dashboards (2-3) with panels of varying suitability:
   - **Good candidate:** `Errors/min by service` (date_histogram + terms + count)
   - **Good candidate:** `Avg latency by endpoint` (date_histogram + terms + avg)
   - **Poor candidate:** `Recent log lines` (raw doc table / top_hits)

**Test criteria:**
- [x] `docker compose up` starts ES + Kibana + log-generator healthy
- [x] Log generator UI accessible at `http://localhost:8090`
- [x] Clicking "Send Batch" with count=1000 ingests exactly 1000 docs into `app-logs`
- [x] Kibana dashboards render with the generated data
- [x] Can send multiple batches and see doc count grow in ES `_cat/indices`
- [x] No logs are sent unless explicitly triggered by the user

---

## Phase 2: Core Domain Model + REST API (CRUD) [COMPLETED]

**Goal:** A running API server that manages `LogMetricRule` resources.

**Deliverables:**
1. Tech choice: **Python + FastAPI** (fast to build, good ES client, typed models)
2. `LogMetricRule` Pydantic model:
   ```
   id, name, owner
   source: { index_pattern, time_field, filter_query }
   group_by: { time_bucket, dimensions[] }
   compute: { type: count|sum|avg|distribution, field?, percentiles?[] }
   backend_config: { type: elastic, retention_days }
   status: draft|active|paused|error
   created_at, updated_at
   ```
3. REST endpoints:
   - `POST /api/rules` — create
   - `GET /api/rules` — list
   - `GET /api/rules/{id}` — get
   - `PUT /api/rules/{id}` — update
   - `DELETE /api/rules/{id}` — delete
4. Storage: SQLite (via SQLModel) — simplest for PoC
5. Basic validation (required fields, enum checks)

**Test criteria:**
- [x] Full CRUD lifecycle via curl/httpie
- [x] Invalid payloads return 422 with clear errors
- [x] Rules persist across server restarts (SQLite)
- [x] `GET /api/rules` returns all created rules

---

## Phase 3: Elasticsearch & Kibana Read-Only Connectors

**Goal:** The service can read index metadata from ES and dashboard definitions from Kibana.

**Deliverables:**
1. **ES Connector** (`es_connector.py`):
   - `list_indices(pattern)` — index names, doc counts, store sizes
   - `get_mapping(index)` — field names + types
   - `get_field_cardinality(index, field)` — approximate distinct count
   - `get_index_stats(index)` — doc count, size, query rate if available
2. **Kibana Connector** (`kibana_connector.py`):
   - `list_dashboards()` — id, title, description
   - `get_dashboard(id)` — full saved object with panels
   - `parse_panels(dashboard)` — extract per-panel: index, query, aggs, visualization type
3. Structured output: each panel parsed into a `PanelAnalysis` object:
   ```
   panel_id, title, index_pattern, time_field
   agg_type (date_histogram, terms, etc.)
   metrics (count, avg, sum, etc.)
   group_by_fields[]
   has_raw_docs: bool
   filter_query
   ```

**Test criteria:**
- [ ] `list_indices("app-logs*")` returns index with correct doc count
- [ ] `get_mapping` returns known fields (service, status_code, etc.)
- [ ] `get_field_cardinality("app-logs", "service")` returns reasonable number
- [ ] `list_dashboards()` returns seeded dashboards
- [ ] `parse_panels()` correctly extracts aggs, group-by fields, index for each panel type

---

## Phase 4: Suitability Scoring + Candidate Analysis

**Goal:** Given a parsed panel, produce a deterministic suitability score (0-100) with a human-readable explanation.

**Deliverables:**
1. **Scoring engine** (`scoring.py`):
   - Structural signals (from panel shape):
     - Uses `date_histogram` → +25
     - Only numeric aggs (count/sum/avg/percentiles) → +20
     - No raw docs / top_hits → +15
     - Group-by fields are keyword/aggregatable → +10
   - Behavioral signals (from usage, if available):
     - Lookback ≥ 7 days → +15
     - Auto-refresh enabled → +10
   - Returns: `SuitabilityScore { total, breakdown[], recommendation_text }`
2. **Dashboard analyzer** (`analyzer.py`):
   - Input: dashboard ID
   - Calls Kibana connector → parse panels → score each
   - Output: `DashboardAnalysis { dashboard_id, panels: PanelScore[] }`
3. API endpoint:
   - `POST /api/analyze/dashboard/{id}` — returns full analysis

**Test criteria:**
- [ ] "Errors/min by service" panel scores >= 80 (high candidate)
- [ ] "Avg latency by endpoint" panel scores >= 60 (candidate)
- [ ] "Recent log lines" panel scores < 30 (not a candidate)
- [ ] Each score includes human-readable explanation text
- [ ] API returns structured JSON with all panels scored

---

## Phase 5: Cost Estimation + Guardrails

**Goal:** Before rule creation, estimate savings and block rules that would increase cost.

**Deliverables:**
1. **Cost estimator** (`cost_estimator.py`):
   - Input: index stats + proposed rule
   - Estimates:
     - Log storage cost (docs/day x retention x avg doc size)
     - Metric storage cost (series_count x retention x point size)
     - Net savings (log cost - metric cost)
     - Query speedup factor (docs scanned -> series scanned)
   - Output: `CostEstimate { log_storage_gb, metric_storage_gb, savings_gb, savings_pct, query_speedup_x }`
2. **Guardrails** (`guardrails.py`):
   - Cardinality check: estimated series count < threshold (e.g. 100K)
   - Dimension limit: max N dimensions per rule (e.g. 5)
   - Net savings > 0 enforced
   - Each check returns: `pass/fail + explanation + suggested fix`
   - Inspired by Datadog: "avoid grouping by unbounded attributes like user_id, request_id, session_id"
3. API integration:
   - `POST /api/estimate` — takes a draft rule, returns cost estimate + guardrail results
   - `POST /api/rules` — now validates guardrails before accepting

**Test criteria:**
- [ ] Rule with 2 low-cardinality dimensions passes all guardrails
- [ ] Rule grouping by `request_id` fails cardinality guardrail with explanation
- [ ] Rule where metric storage > log storage fails net-savings guardrail
- [ ] Cost estimate returns plausible numbers for seeded data
- [ ] Guardrail failures include actionable suggested fixes

---

## Phase 6: Elastic Metrics Backend (Transform Provisioning)

**Goal:** A created rule actually materializes metrics in Elasticsearch via continuous transforms.

**Deliverables:**
1. **MetricsBackend interface** (`backend.py`):
   ```python
   class MetricsBackend(ABC):
       def validate(rule) -> ValidationResult
       def provision(rule) -> ProvisionResult
       def get_status(rule_id) -> BackendStatus
       def deprovision(rule_id) -> None
   ```
2. **ElasticMetricsBackend** (`elastic_backend.py`):
   - `provision(rule)`:
     - Creates target metrics index (or TSDS) with appropriate mapping
     - Creates continuous transform: pivot by time_bucket + dimensions, compute aggs
     - Applies ILM policy: longer retention on metrics index
     - Starts transform
   - `get_status(rule_id)`: returns transform health, docs processed, last checkpoint
   - `deprovision(rule_id)`: stops + deletes transform, optionally deletes index
3. Rule lifecycle integration:
   - Creating a rule with `status: active` triggers `provision()`
   - Deleting a rule triggers `deprovision()`
   - `GET /api/rules/{id}/status` returns backend health

**Test criteria:**
- [ ] Creating an active rule provisions an ES transform
- [ ] Transform processes existing logs into metrics index
- [ ] Metrics index contains expected aggregated documents (correct time buckets, dimensions, values)
- [ ] `get_status` returns running transform with processed doc count > 0
- [ ] Deleting a rule removes the transform cleanly
- [ ] Querying metrics index is faster than equivalent log aggregation

---

## Phase 7: Portal UI (Minimal End-to-End)

**Goal:** A simple web UI that ties the full flow together.

**Deliverables:**
1. Tech: **React + Vite** (TypeScript)
2. Pages/views:
   - **Dashboard list** — connect to Kibana, show dashboards
   - **Dashboard analysis** — table of panels with scores, savings estimates, actions
   - **Create metric rule** — wizard pre-filled from panel, shows guardrail results inline
   - **Rules list** — active rules with status, backend health
3. End-to-end flow:
   - Select dashboard -> see panel analysis table -> click "Create metric" on a high-scoring panel -> review pre-filled rule -> see guardrail validation -> confirm -> rule created -> transform running

**Test criteria:**
- [ ] Can browse and select a Kibana dashboard from the portal
- [ ] Panel analysis table shows scores, savings, and recommendations
- [ ] Clicking "Create metric" opens pre-filled wizard
- [ ] Guardrail violations shown inline with suggestions
- [ ] Successful rule creation shows transform running status
- [ ] End-to-end: dashboard -> analysis -> rule -> verified metrics in ES

---

## Tech Stack Summary (PoC)

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Backend API | Python + FastAPI | Typed, fast to build, good ES libraries |
| Domain models | Pydantic + SQLModel | Validation + persistence in one |
| Storage | SQLite | Zero-config, sufficient for PoC |
| ES client | elasticsearch-py | Official client |
| Kibana access | HTTP REST (saved objects API) | No plugin needed |
| Frontend | React + Vite | Component-based, fast dev server |
| Infrastructure | Docker Compose | ES + Kibana + API + UI |
