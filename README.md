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

## Rule Configuration

Each rule has two timing settings that control the underlying ES continuous transform:

| Setting | UI Label | Default | What it controls |
|---------|----------|---------|-----------------|
| `time_bucket` | Bucket | *from panel* | The time granularity for metric aggregation (e.g. `1m`, `5m`). Auto-filled from the panel's date_histogram interval when available. Falls back to `1m` when the panel uses Kibana's auto-interval. Larger buckets produce fewer metric docs (cheaper storage), smaller buckets give finer resolution. |
| `frequency` | Check Interval | `auto` | How often the transform checks for new data to process. `auto` picks `max(time_bucket, 1m)`. Lower = fresher metrics but more ES overhead. |
| `sync_delay` | Late Data Buffer | `30s` | How long the transform waits behind real-time before sealing a time bucket. This buffer exists because events can arrive in ES after the moment they occurred — due to log shipper batching, network retries, or ES indexing delays. Once a bucket is sealed, late-arriving events are **silently dropped**. Set this to exceed your worst-case log pipeline delay. |

**Bucket auto-fill**: When you analyze a panel in Step 3, the Bucket dropdown is pre-selected from the panel's actual date_histogram interval (marked with `*`). This ensures the metrics match the original chart's resolution. You might still want to change it — for example, a panel using `10s` buckets produces 6x more metric docs than `1m`. If nobody needs 10-second granularity, choosing `1m` saves significant storage.

**Auto-interval vs fixed bucket**: Many Kibana panels use auto-interval, where the bucket size changes dynamically based on the time range (e.g. ~30s for a 1-hour view, ~3h for a 30-day view). The transform cannot do this — it needs a fixed interval baked in at creation time. This interval sets the **floor of resolution**: you can always aggregate up at query time (e.g. query 1m metric docs at 1h granularity), but you can never go finer than what's stored. When auto-interval is detected, the UI defaults to `1m` — fine enough for most operational dashboards without excessive storage cost.

**How to choose a Late Data Buffer value**: If your logs reliably land in ES within 10 seconds of their timestamp, `10s` is safe. The `30s` default adds a safety margin for pipeline hiccups (shipper restarts, ES backpressure, network blips). The tradeoff is purely latency — metrics appear behind real-time by this amount.

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
| `GET` | `/api/health` | Health monitor status + rules in error |
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
python -m pytest -v    # 167 tests, no Docker required
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
    tests/                  # 167 unit/integration tests
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
