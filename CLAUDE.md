# CLAUDE.md — Coding Standards & Quick Reference

> Auto-loaded by Claude Code. Keep this lean — only actionable rules and references.
> For architecture, API endpoints, and domain model see [ARCHITECTURE.md](ARCHITECTURE.md).
> For project history and bug post-mortems see [CHANGELOG.md](CHANGELOG.md).

## Project Overview

Application teams emit structured logs to Elasticsearch and build Kibana dashboards that repeatedly aggregate those same logs (count errors per minute, average latency by endpoint, etc.). Every dashboard load re-scans millions of raw log documents to compute the same aggregations — expensive in storage and slow at scale.

Logs2Metrics automates converting those repeated aggregations into pre-computed metrics. It analyzes Kibana dashboards, scores each panel for metric conversion suitability, checks cost guardrails (cardinality, storage savings), provisions ES continuous transforms that materialize aggregations into small metrics indices, and clones the original visualizations to read from the pre-computed data. Same charts, same look — orders of magnitude fewer documents queried. No application code changes required. All phases complete.

**Goal check**: Every task should serve the core mission — reducing storage cost and query time by converting log aggregations into pre-computed metrics. Push back on features, abstractions, or complexity that don't directly support this.

**Audience**: This project ships to an on-prem environment and will be maintained by a team of junior engineers. Optimize for readability and simplicity:
- Prefer explicit over clever. Obvious code > elegant code.
- Prefer flat over nested. Avoid deep abstractions or indirection layers that require jumping between files to understand a flow.
- Name things for clarity, not brevity. A long descriptive name is better than a short ambiguous one.
- Keep dependencies minimal. Every added library is something the team needs to learn and maintain.
- Comments should explain *why*, not *what*. If the *what* isn't obvious, simplify the code.

**Stack**: Docker Compose (ES 8.12 + Kibana 8.12 + FastAPI + Log Generator), SQLite, single-page portal UI

**Key files**:
- `api/main.py` — FastAPI endpoints, proxy routing, connection handling
- `api/elastic_backend.py` — ES transform provisioning and metrics index management
- `api/kibana_connector.py` — Kibana read/write operations (dashboards, visualizations, data views)
- `api/debug_ui.html` — Self-contained portal UI (inline JS, no framework)
- `api/models.py` — Pydantic/SQLModel domain models

**Doc maintenance**: After completing work, update docs to stay in sync:
- `CHANGELOG.md` — Add an entry for any new feature, bug fix, or significant change
- `ARCHITECTURE.md` — Update if project structure, API endpoints, or components changed
- `README.md` — Update if stack, quick start steps, API surface, or project structure changed
- This file — Update if new coding standards or gotchas were discovered

## Running Tests

```bash
python -m pytest -v          # 135 tests, no Docker required
```

---

## Coding Standards

### JavaScript / Portal UI (`debug_ui.html`)

1. **Never use `innerHTML +=` in a loop.** Build the complete HTML string first, assign once with `innerHTML =`, then interact with child elements.

2. **Always use the `api()` wrapper for backend calls.** Never use raw `fetch()`. The wrapper handles `X-Kibana-Url`/auth header injection, JSON parsing, 204 No Content, and error extraction.

3. **Keep state and views in sync.** When updating any `state.*` property, also update all dependent UI elements.

4. **Use `clearTimeout` not `clearInterval` for polling.** Our polling uses recursive `setTimeout`.

5. **Map Docker-internal URLs to browser-accessible URLs.** Use `_KIBANA_URL_MAP` for "View in Kibana" links.

### Python / Backend

6. **Never use ES reserved field names** in index mappings or transform outputs. Reserved: `doc_count`, `_source`, `_id`, `_type`, `_index`, `_score`, `_routing`. Use `event_count` instead of `doc_count`.

7. **Never swallow API errors silently.** Log and propagate 4xx/5xx. A swallowed error in step N causes a confusing error in step N+1.

8. **Prefer atomic batch operations.** Include all referenced objects (data views, visualizations) in one NDJSON import batch rather than separate API calls.

9. **Apply patterns symmetrically.** When adding `es_auth`, also add `kibana_auth`. When creating a helper for one direction, ensure the complement follows the same pattern.

10. **Migrate ALL call sites when introducing a wrapper.** Grep for direct usage of the underlying API and convert them.

---

## Design Standards

1. **Design poll/wait conditions for edge cases.** Handle zero-match docs, slow checkpoints, transforms green without indexing. Prefer lenient stop conditions.

2. **Test with all supported configurations.** Auth bugs only manifest on security-enabled instances.

3. **Test data correctness, not just status codes.** Assert on actual values, not just HTTP 200.

4. **Test with N > 1.** DOM and state bugs often only manifest with 2+ items.

5. **Include `allowNoIndex: true` on data views** when the backing index may not exist yet.

6. **Return structured errors** with original downstream error messages.

7. **Handle 204 No Content in API clients.** Check status before calling `response.json()`.

8. **Treat the DOM as a derived view of state.** Single source of truth in `state.*`.

9. **When extending a service map, update ALL entries.** Partial updates are bugs.

10. **Decouple creation from dependency order.** Verify step N succeeded before step N+1, or use atomic batches.

---

## ES / Kibana Gotchas

| Gotcha | Details |
|--------|---------|
| `doc_count` is reserved | ES uses it for `_doc_count_field_name`. Use `event_count`. |
| Data view REST API is fragile | Returns 400 (not 404) for missing indices. Prefer NDJSON import. |
| Kibana NDJSON references | Must be resolvable within the import batch or pre-existing. |
| `httpx` doesn't follow redirects | Use `httpx.Client(follow_redirects=True)`. |
| `SQLModel.create_all` won't alter | Only creates new tables. Use manual `ALTER TABLE`. |
| Kibana panel reference names | Dashboard prefixes with `{panelIndex}:` but panels store without prefix. |
| `searchSourceJSON` is required | Dashboard attributes MUST include it or Kibana crashes. |
| Lens panels hard to create via API | Legacy `visualization` saved objects with `visState` + `aggs` are more reliable. |
| Continuous transforms are forward-only | Only process docs in time buckets AFTER the checkpoint. The 24h initial generation seals all past buckets. Injected events must be at exactly `now` (current open bucket) — even 30s in the past can land in a sealed bucket. |
| Transform `sync.time.delay` is baked in | Set at creation time. Changing the value in code only affects new rules — existing must be deleted and recreated. |
r