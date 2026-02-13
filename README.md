# Logs2Metrics

Convert Kibana dashboard aggregations into pre-computed Elasticsearch metrics — same charts, faster queries, less storage.

## The Problem

Application teams emit structured logs to Elasticsearch and build Kibana dashboards that repeatedly aggregate those logs (count errors per minute, average latency by endpoint, etc.). Every dashboard load re-scans millions of raw documents to compute the same aggregations. This is expensive in storage and slow at scale.

## The Solution

Logs2Metrics analyzes your existing Kibana dashboards, identifies panels suitable for metric conversion, and provisions ES continuous transforms that materialize the aggregations into small, fast metrics indices. It then clones the original visualizations to read from the pre-computed data. No application code changes required.

```
  Kibana Dashboard                Logs2Metrics                    Metrics Dashboard
  ┌──────────────┐    analyze    ┌──────────────┐   provision   ┌──────────────┐
  │ Errors/min   │───────────>│  Score panels  │────────────>│ Same chart,  │
  │ Avg latency  │            │  Cost guard    │             │ reads from   │
  │ Raw logs     │            │  Create rules  │             │ metrics idx  │
  └──────┬───────┘            └──────────────┘             └──────┬───────┘
         │                                                         │
    scans millions                                           scans thousands
    of log docs                                              of metric docs
```

## Quick Start

```bash
# Start the stack
docker compose up -d --build

# Wait for Kibana to be healthy (~30s), then seed dashboards
cd seed-dashboards
pip install -r requirements.txt
python seed.py --kibana http://localhost:5602
```

Open the portal at **http://localhost:8091/debug** and follow the 5-step walkthrough:

1. **Generate logs** — Create synthetic log data
2. **View raw logs** — See what's in Elasticsearch
3. **Analyze panels** — Score each dashboard panel for metric conversion suitability
4. **Create rules** — Provision ES transforms that materialize metrics
5. **Compare** — Side-by-side: log aggregation query vs pre-computed metrics query

Swagger API docs: **http://localhost:8091/docs**

## Stack

| Service | Port | Description |
|---------|------|-------------|
| Elasticsearch 8.12 | 9201 | Log storage + metrics indices |
| Kibana 8.12 | 5602 | Dashboard visualization |
| API (FastAPI) | 8091 | Control plane + portal UI |
| Log Generator (FastAPI) | 8090 | Synthetic log data |

All services run via Docker Compose. Data is stored in SQLite (rules) and Elasticsearch (logs + metrics).

## How It Works

1. **Analyze** — Reads Kibana dashboard panels, extracts aggregation structure, scores suitability (0-95) based on 6 signals
2. **Guard** — Validates cost guardrails before creating rules: cardinality limits, dimension count, net storage savings
3. **Provision** — Creates an ES continuous transform per rule: ILM policy, metrics index, transform definition, auto-start
4. **Visualize** — Clones original Kibana visualizations, rewires them to read from metrics indices, adds to a metrics dashboard
5. **Compare** — Runs log aggregation and metrics query side-by-side to show doc reduction and query speedup

## API Overview

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/rules` | Create a metric rule (with guardrail validation) |
| `GET` | `/api/rules` | List all rules |
| `GET` | `/api/rules/{id}` | Get rule details |
| `PUT` | `/api/rules/{id}` | Update rule (handles status transitions) |
| `DELETE` | `/api/rules/{id}` | Delete rule (deprovisions transform) |
| `GET` | `/api/rules/{id}/status` | Live transform health + stats |
| `POST` | `/api/analyze/dashboard/{id}` | Score all panels for metric suitability |
| `POST` | `/api/estimate` | Cost estimate + guardrail check for a draft rule |
| `POST` | `/api/metrics-dashboard` | Create a Kibana metrics dashboard |
| `POST` | `/api/metrics-dashboard/panels/{rule_id}` | Add rule panel to metrics dashboard |
| `GET` | `/api/kibana/dashboards` | List Kibana dashboards |
| `GET` | `/api/kibana/test-connection` | Test Kibana connectivity |

All Kibana-related endpoints accept `X-Kibana-Url`, `X-Kibana-User`, `X-Kibana-Pass` headers for multi-instance support.

Full endpoint reference: [ARCHITECTURE.md](ARCHITECTURE.md#api-endpoints)

## Portal UI

The self-service portal at `/debug` has two tabs:

- **Pipeline** — 5-step guided walkthrough: generate logs, analyze dashboards, create rules, compare results
- **Rules Manager** — Persistent rule management: view, edit, compare, activate/pause, delete, create metrics dashboards

Connect to any Kibana instance (with optional auth) directly from the portal header.

## Tests

```bash
pip install -r requirements-test.txt -r api/requirements.txt
python -m pytest -v    # 135 tests, no Docker required
```

All external dependencies (ES, Kibana) are mocked. Tests cover model validation, scoring engine, cost estimator, guardrails, transform provisioning, API endpoints, and static analysis checks.

## Project Structure

```
logs2metrics/
  docker-compose.yml        # ES + Kibana + Log Generator + API
  api/
    main.py                 # FastAPI endpoints + proxy routing
    models.py               # Domain models (LogMetricRule)
    elastic_backend.py      # ES transform provisioning
    kibana_connector.py     # Kibana read/write operations
    scoring.py              # Panel suitability scoring
    guardrails.py           # Pre-creation validation
    cost_estimator.py       # Storage cost comparison
    debug_ui.html           # Portal UI (self-contained)
    tests/                  # 135 unit/integration tests
  log-generator/            # Synthetic log data service
  seed-dashboards/          # Kibana dashboard seeder
```

## Documentation

| File | Purpose |
|------|---------|
| [README.md](README.md) | This file — project overview and quick start |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Technical reference: components, API endpoints, domain model, data flow |
| [CLAUDE.md](CLAUDE.md) | Coding standards and project conventions |
| [CHANGELOG.md](CHANGELOG.md) | Project history: completed phases and bug post-mortems |
