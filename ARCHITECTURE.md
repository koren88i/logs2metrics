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
- `POST /generate { count }` — creates a batch of structured logs
- `GET /status` — last batch result
- Logs spread across last 24h for realistic dashboard rendering

### Seed Dashboards (`seed-dashboards/`)
- Python script using Kibana saved objects NDJSON import API
- Idempotent (fixed IDs, `overwrite=true`)

### API Service (`api/`)
- FastAPI + SQLModel + SQLite
- `LogMetricRule` CRUD at `/api/rules`
- Swagger UI at `/docs`

### Planned (Not Yet Implemented)
- ES + Kibana read-only connectors (Phase 3)
- Suitability scoring engine (Phase 4)
- Cost estimator + guardrails (Phase 5)
- ElasticMetricsBackend — ES transforms (Phase 6)
- Portal UI — React + Vite (Phase 7)

---

## Data Flow

```
1. ANALYZE (read-only)
   Kibana Dashboard --> parse panels --> score suitability --> show recommendations

2. CREATE RULE (user action)
   Panel recommendation --> pre-filled rule --> guardrail validation --> save rule

3. PROVISION (automated)
   Saved rule --> ES continuous transform --> metrics index + ILM policy

4. QUERY (user benefit)
   Dashboard query --> hits metrics index (fast) instead of log index (slow)
```

---

## Project Structure

```
logs2metrics/
  docker-compose.yml          # ES + Kibana + log-generator + api
  PLAN.md                     # Phased implementation plan
  STATUS.md                   # Current state & handoff notes
  ARCHITECTURE.md             # This file
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
    main.py                   # FastAPI CRUD endpoints
    models.py                 # LogMetricRule SQLModel + Pydantic schemas
    database.py               # SQLite engine + session
  ui/                         # [Phase 7] React + Vite frontend
```

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
  compute:
    type: count|sum|avg|distribution
    field: string?              # required for sum/avg/distribution
    percentiles: float[]?       # e.g. [50, 90, 95, 99]
  backend_config:
    type: elastic               # future: prometheus
    retention_days: int         # default 450
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
