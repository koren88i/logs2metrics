# Logs2Metrics PoC - Status & Session Context

> Handoff document for new conversations.
> Read this + PLAN.md + ARCHITECTURE.md to continue implementation.

---

## Current Phase: Phase 2 COMPLETED — Next: Phase 3

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

---

## Next Step: Phase 3

Implement ES + Kibana read-only connectors inside the `api/` service.
See PLAN.md Phase 3 for full spec.

Key files to create:
- `api/es_connector.py` — index metadata, mappings, cardinality
- `api/kibana_connector.py` — dashboard listing, panel parsing
