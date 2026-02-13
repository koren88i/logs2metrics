# CLAUDE.md — Project Memory & Standards

> Read by Claude Code at the start of every conversation.
> Contains lessons learned, coding standards, and design principles extracted from post-Phase 7 bug hunts.

---

## Project Overview

Logs2Metrics is a platform service that derives metrics from existing Elasticsearch logs by analyzing Kibana dashboards. See [ARCHITECTURE.md](ARCHITECTURE.md) for full architecture and [STATUS.md](STATUS.md) for current state.

**Stack**: Docker Compose (ES 8.12 + Kibana 8.12 + FastAPI API + Log Generator), SQLite, single-page portal UI (`debug_ui.html`)

**Key files**:
- `api/main.py` — FastAPI endpoints, proxy routing, connection handling
- `api/elastic_backend.py` — ES transform provisioning and metrics index management
- `api/kibana_connector.py` — Kibana read/write operations (dashboards, visualizations, data views)
- `api/debug_ui.html` — Self-contained portal UI (inline JS, no framework)
- `api/models.py` — Pydantic/SQLModel domain models

---

## Bug Catalog: What Went Wrong & Why

### Bug 1: `innerHTML +=` Destroying DOM References
- **Symptom**: Rules Manager showed "Checking..." forever for all rules except the last
- **Root cause**: `container.innerHTML += cardHtml` in a loop re-serializes and re-parses the entire container on each iteration. All previously captured DOM element references (from `getElementById`) become detached nodes. Only the last rule's elements survived because no further `+=` invalidated them.
- **Fix**: Build all card HTML as a single string, assign once with `container.innerHTML = allCards`, then start polling after all DOM elements are stable.
- **Why tests missed it**: No DOM/UI tests exist. Single-rule manual testing wouldn't reveal it since the last rule always works. The bug only manifests with 2+ rules.
- **Category**: DOM mutation anti-pattern

### Bug 2: Dashboard Selector Not Updating Panels
- **Symptom**: Changing the dashboard dropdown in the pipeline didn't reload Step 3 panels
- **Root cause**: `onDashboardChange()` updated `state.dashboardId` but never called `loadPanels()`. State changed without updating the dependent view.
- **Fix**: Added `loadPanels()` call when Step 3 is already unlocked.
- **Why tests missed it**: No UI interaction tests. Manual testing only covered the initial flow (select dashboard -> unlock steps), not re-selecting after Step 3 was already open.
- **Category**: Incomplete state synchronization

### Bug 3: Kibana 401 on Security-Enabled Instances
- **Symptom**: Creating a metrics dashboard on kibana2 (security enabled) returned 401
- **Root cause**: `_KIBANA_SERVICE_MAP` had `es_auth` for kibana2 but no `kibana_auth`. ES auth was auto-filled but Kibana auth was missed. The pattern was applied asymmetrically.
- **Fix**: Added `kibana_auth` to the service map; `get_kibana_conn` auto-fills Kibana credentials for known instances (mirroring how `_get_es_client` already works for ES).
- **Why tests missed it**: No integration tests against the security-enabled stack. The second Kibana was added for testing but the service map was only partially updated.
- **Category**: Incomplete feature parity when extending a pattern

### Bug 4: NDJSON `missing_references` on Add Panel
- **Symptom**: "Add Panel" failed with `missing_references` for some rules
- **Root cause**: Two-phase failure: (1) `_create_data_view()` via REST API silently returned 400 when the metrics index didn't exist yet, (2) the subsequent NDJSON import (visualization + dashboard referencing the data view) failed because the data view was never created. The 400 was swallowed.
- **Fix**: Include the data view as an `index-pattern` saved object directly in the NDJSON import batch. References resolve within the batch atomically.
- **Why tests missed it**: Testing was done with rules whose metrics indices already existed. The race condition between transform provisioning and panel creation was never tested. The swallowed 400 masked the real problem.
- **Category**: Silent failure + race condition

### Bug 5: `doc_count` Reserved Field Name (Count = 1)
- **Symptom**: Count values showed 1 instead of the actual count (e.g., 6) in metrics dashboards and comparisons
- **Root cause**: The transform output field was named `doc_count`, which is an ES reserved internal field (`_doc_count_field_name` metadata). ES treats `doc_count` as controlling the document count for aggregation buckets. `sum(doc_count)` returns the number of bucket documents (1), not the stored numeric value.
- **Fix**: Renamed to `event_count` across `elastic_backend.py`, `kibana_connector.py`, and `debug_ui.html`.
- **Why tests missed it**: No end-to-end test that verifies actual numeric values after transform execution. The naming conflict only manifests at ES aggregation query time, not at index time.
- **Category**: Reserved name collision

### Bug 6: Raw `fetch()` Bypassing Connection Headers
- **Symptom**: Delete operations in Rules Manager didn't route to the correct backend when connected to a non-default Kibana
- **Root cause**: `mgrDeleteRule()` and `cleanup()` used raw `fetch()` instead of the `api()` wrapper. The `api()` helper injects `X-Kibana-Url`/auth headers; raw `fetch()` doesn't. These call sites predated the multi-Kibana feature and weren't migrated.
- **Fix**: Replaced `fetch()` with `api()`. Added 204 No Content handling to `api()`.
- **Why tests missed it**: No multi-connection testing. No lint/static analysis to enforce "always use `api()` not `fetch()`".
- **Category**: Inconsistent API wrapper usage

### Bug 7: "Checking..." Forever on Zero-Match Transforms
- **Symptom**: Active rules with no matching docs (e.g., error filter with all-200 data) showed "Checking..." for 60s
- **Root cause**: Poll stop condition required `health === 'green' && last_checkpoint`. A transform with zero matching documents reaches green health but may not produce a checkpoint quickly.
- **Fix**: Show whatever the backend returns immediately. Only continue polling for `yellow` (transitioning) state.
- **Why tests missed it**: Only tested with data that matched the filter. Zero-match edge case was never considered.
- **Category**: Overly strict success criteria

---

## Why Tests Didn't Catch These (Systemic Gaps)

1. **No UI/DOM tests** — The portal is a single HTML file with inline JS. No testing framework (Jest, Playwright, etc.) is set up. All DOM-related bugs (1, 2) were invisible.
2. **No end-to-end integration tests** — No test creates a rule, waits for the transform to checkpoint, then verifies the actual metric values. Data correctness bugs (5) slipped through.
3. **No multi-configuration tests** — Tests only ran against the default no-security stack. Auth bugs (3) and routing bugs (6) only manifested with the second Kibana.
4. **No edge-case coverage** — Zero-match filters (7), missing indices (4), race conditions between provisioning and querying — none were tested.
5. **No data correctness assertions** — Tests checked HTTP status codes but not actual numeric values in the response bodies.
6. **No static analysis for UI code** — No ESLint or equivalent to enforce patterns like "use `api()` not `fetch()`". No way to catch stale call sites after introducing a wrapper.
7. **Swallowed errors hid root causes** — Silent 400 responses (4) meant the real failure was masked by a confusing downstream error.

---

## Coding Standards

### JavaScript / Portal UI (`debug_ui.html`)

1. **Never use `innerHTML +=` in a loop.** Build the complete HTML string first, assign it once with `innerHTML =`, then interact with child elements. This prevents DOM reference invalidation.

2. **Always use the `api()` wrapper for backend calls.** Never use raw `fetch()`. The wrapper handles:
   - `X-Kibana-Url` / `X-Kibana-User` / `X-Kibana-Pass` header injection
   - JSON parsing
   - 204 No Content responses (returns `null`)
   - Error extraction

3. **Keep state and views in sync.** When updating any `state.*` property, also update all UI elements that depend on that state. If `state.dashboardId` changes, anything derived from it (panel list, analysis results) must be refreshed.

4. **Use `clearTimeout` not `clearInterval` for polling.** Our polling pattern uses recursive `setTimeout`, not `setInterval`. Mismatched clear calls silently fail.

5. **Map Docker-internal URLs to browser-accessible URLs.** Use `_KIBANA_URL_MAP` for any "View in Kibana" links. Docker-internal hostnames (`kibana:5601`) are not reachable from the browser.

### Python / Backend

6. **Never use ES reserved field names in index mappings or transform outputs.** Known reserved names: `doc_count`, `_source`, `_id`, `_type`, `_index`, `_score`, `_routing`. Use descriptive alternatives (e.g., `event_count` instead of `doc_count`).

7. **Never swallow API errors silently.** If a REST call returns 4xx/5xx, log it and propagate it. A swallowed error in step 1 of a multi-step operation will cause a confusing error in step 2.

8. **Prefer atomic batch operations over multi-step sequences.** When Kibana NDJSON import references objects (data views, visualizations), include them all in the same import batch rather than creating them in separate API calls. The batch resolves references internally.

9. **Apply patterns symmetrically.** When adding `es_auth` to a service map, also add `kibana_auth`. When creating a helper for one direction (e.g., `_get_es_client`), ensure the complementary direction (`get_kibana_conn`) follows the same pattern.

10. **When introducing a wrapper/abstraction, migrate ALL existing call sites.** Grep for direct usage of the underlying API (`fetch(`, `httpx.get(`, etc.) and convert them. Unmigrated call sites become bugs when the wrapper adds new behavior (auth headers, routing, error handling).

---

## Design Standards

### Robustness

1. **Design poll/wait conditions for edge cases.** Stop conditions must handle: zero matching documents, slow checkpointing, transforms that go green without indexing anything. Prefer lenient conditions (stop on "not transitioning") over strict ones (stop on "green AND checkpointed AND docs > 0").

2. **Test with all supported configurations.** If the system supports N Kibana instances with different security settings, test all code paths against all configurations. Auth bugs only manifest on security-enabled instances.

3. **Test data correctness, not just API status codes.** A 200 response with `count: 1` instead of `count: 6` is a silent data corruption bug. Always assert on actual values in end-to-end tests.

4. **Test with N > 1.** Many DOM and state management bugs only manifest with multiple items (2+ rules, 2+ panels, 2+ dashboards). Always test with at least 2 of everything.

### API Design

5. **Include `allowNoIndex: true` on data views** when the backing index may not exist yet (e.g., metrics indices created by transforms that haven't checkpointed).

6. **Return structured errors, not just status codes.** Include the original error message from downstream services (ES, Kibana) so the user (or developer) can diagnose the root cause.

7. **Handle 204 No Content in API clients.** `response.json()` on a 204 will throw. Always check status before parsing.

### State Management

8. **Treat the DOM as a derived view of state.** Don't store state in the DOM (reading values from elements you just created). Keep a single source of truth in `state.*` and render from it.

9. **When extending a service map or config, update ALL entries.** A partial update (adding `es_auth` but not `kibana_auth`) is a bug waiting to happen.

10. **Decouple creation order from dependency order.** If object B references object A, either create A first (and verify it succeeded) or include both in an atomic batch. Never assume A exists because you called the create API — it may have silently failed.

---

## ES / Kibana Gotchas (Quick Reference)

| Gotcha | Details |
|--------|---------|
| `doc_count` is reserved | ES uses it for `_doc_count_field_name`. Use `event_count`. |
| Data view REST API is fragile | Returns 400 (not 404) for missing indices. Prefer NDJSON import with `index-pattern` type. |
| `innerHTML +=` destroys DOM | Classic JS pitfall. Build string first, assign once. |
| Kibana NDJSON references | Must be resolvable within the import batch or pre-existing. |
| `httpx` doesn't follow redirects | Use `httpx.Client(follow_redirects=True)`. |
| `SQLModel.create_all` won't alter | Only creates new tables. Use manual `ALTER TABLE` for new columns. |
| Kibana panel reference names | Dashboard prefixes with `{panelIndex}:` but panels store without prefix. Check both. |
| `searchSourceJSON` is required | Dashboard attributes MUST include it or Kibana crashes. |
| Lens panels are hard to create via API | Legacy `visualization` saved objects with `visState` + `aggs` are more reliable. |

---

## Test Suite

**135 tests across 12 files**, all passing. Run with `python -m pytest -v` from the repo root. No Docker required — all external dependencies are mocked.

```
pytest.ini                              # Config: testpaths, pythonpath, markers
requirements-test.txt                   # pytest + pytest-cov + httpx
api/tests/
  conftest.py                           # Shared fixtures: factories, mocks, TestClient
  test_models.py                        # 19 tests — Pydantic validation (SourceConfig, GroupBy, Compute, etc.)
  test_scoring.py                       # 22 tests — All 6 scoring signals + max score + recommendations
  test_cost_estimator.py                # 12 tests — Cost math, series count, cardinality fallback, bucket parsing
  test_guardrails.py                    # 11 tests — dimension_limit, cardinality, high_cardinality_fields, net_savings
  test_elastic_backend.py               # 16 tests — Transform body, field naming (Bug 5), status, frequency logic
  test_kibana_connector.py              #  9 tests — Vis cloning, NDJSON batch with data view (Bug 4), format
  test_api_rules.py                     # 14 tests — CRUD endpoints (create, list, get, update, delete)
  test_api_status.py                    #  4 tests — Backend status, zero-doc handling (Bug 7)
  test_api_errors.py                    #  3 tests — Health, provision failure, estimate endpoint
  test_service_map.py                   #  7 tests — Auth parity (Bug 3), auto-fill, override
  test_static_analysis.py               #  4 tests — No raw fetch (Bug 6), no doc_count (Bug 5), no innerHTML+= in loops (Bug 1)
```

**Bug-to-test mapping**: Each of the 7 post-Phase-7 bugs has at least one regression test. See the plan file for the full mapping table.

**What's NOT covered yet** (future work):
1. **End-to-end data correctness tests** — Create rule, wait for transform, query metrics index, assert actual numeric values. Requires Docker stack.
2. **Multi-item UI tests** — Render 3+ rules/panels in a browser, verify all are interactive. Requires Playwright or similar.
3. **Multi-configuration integration tests** — Run against both default and security-enabled Kibana. Requires Docker stack.
4. **Edge case tests with real ES** — Zero-match filters, missing indices, race conditions between provisioning and querying.
