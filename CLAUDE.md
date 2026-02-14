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

**Stack**: Docker Compose (ES 8.12 + Kibana 8.12 + FastAPI), SQLite, single-page portal UI

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
python -m pytest -v          # 196 tests, no Docker required
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

## Bug Investigation Methodology

When something doesn't work as expected, **do not jump to the first plausible explanation**. Follow this sequence:

1. **Characterize the symptom precisely.** What exactly is wrong? Which specific data is missing/wrong? What data IS correct? Write it down.

2. **Look at the pattern.** The shape of what's wrong tells you the category of bug:
   - Missing data: *which* data? Earlier events? Later ones? Random? The pattern rules out entire categories. (e.g., "earlier events dropped but later ones processed" rules out timing/delay — if it were delay, later events would be dropped.)
   - Wrong values: off by a constant factor? Always zero? Only wrong for specific inputs?
   - Intermittent: timing-dependent? Load-dependent? Configuration-dependent?

3. **List ALL possible causes before investigating any.** For "data not processed," possible causes include: timing/delay, structural (sealed buckets, wrong index), filtering (query mismatch), permissions, wrong endpoint, data never written, data written to wrong location, etc.

4. **Eliminate causes using the pattern from step 2.** The pattern should rule out most causes immediately, before you read a single line of code.

5. **Distinguish code bugs from mental model bugs.** If the code does exactly what you wrote but the result is wrong, the bug is in your understanding of the external system (ES, Kibana, etc.) — not in the code. These are harder: you need to verify your assumptions about how the external system works, not just re-read your own logic.

**Anti-pattern**: Assuming the first hypothesis is correct and iterating on fixes without disproving alternatives. This leads to a chain of patches addressing symptoms of a misdiagnosis.

---

## Test Strategy

The current test suite (196 tests) mocks all external services (ES, Kibana, log-generator). This means:
- **What it validates**: Our code does what we wrote — correct API contracts, model validation, error handling, static patterns.
- **What it cannot validate**: Whether our assumptions about external system behavior are correct.

Every mock encodes an assumption. If the assumption is wrong, the mock confirms our wrong mental model. The sealed-bucket bug is the example: we assumed events with timestamps 30s in the past would be processed. No unit test could catch this because our mocks would process them.

**Gaps to be aware of**:
- Behavioral assumptions about ES (checkpoint semantics, bucket sealing, reserved field names) are untested.
- End-to-end data flow (generate → transform → query metrics) is never validated against real ES.
- UI interaction sequences (multi-step pipeline) have no automated coverage.

When adding features that depend on external system behavior, explicitly document the behavioral assumption in code comments and consider whether it can be verified.

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
| Transform `sync.time.delay` is baked in | Set at creation time. Now configurable per rule via `sync_delay` field (default `30s`). Editing delay on an active rule auto-reprovisions. |
| Transform `time_bucket` is fixed, Kibana's is dynamic | Kibana auto-interval changes based on time range (~30s for 1h view, ~3h for 30d view). The transform needs a fixed interval baked at creation. This sets the floor of resolution — queries can aggregate up (1m→1h) but never finer. |