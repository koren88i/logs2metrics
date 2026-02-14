# Research: Datadog Logs-to-Metrics vs. Logs2Metrics

> Comparison conducted 2026-02-14. Sources listed at bottom.

---

## What Datadog Offers

Datadog's "Generate Metrics from Ingested Logs" lets users define log-based metrics via a simple form: a log query filter, group-by tags (dimensions), and a metric type (count or distribution). Metrics are generated **at ingest time** — as logs flow through the pipeline, matching logs produce metric data points at 10-second granularity, retained for 15 months.

Key characteristics:
- **Metric types**: Count and Distribution (distributions include percentiles, sum, avg, min, max)
- **Granularity**: Fixed 10-second intervals
- **Processing**: Real-time at ingest — zero delay between log arrival and metric availability
- **Cardinality control**: Implicit via billing ($5/100 custom metrics/month over allotment); "Metrics without Limits" allows post-hoc tag pruning
- **Dashboard integration**: Manual — user must create new widgets pointing at generated metrics
- **Backfill**: Not supported — forward-only, same as ES transforms
- **Advanced**: Observability Pipelines can generate metrics pre-ingestion at the edge

## What Logs2Metrics Offers

Logs2Metrics analyzes existing Kibana dashboards, scores panels for conversion suitability, validates cost guardrails, provisions ES continuous transforms, and clones visualizations to read from pre-computed metrics indices.

Key characteristics:
- **Metric types**: Count, Sum, Avg, Distribution (with configurable percentiles)
- **Granularity**: User-configurable (10s / 1m / 5m / 10m / 1h), auto-filled from panel interval
- **Processing**: ES continuous transforms polling on configurable frequency (minimum ~1m)
- **Cardinality control**: 4 explicit pre-creation guardrails (dimension limit, cardinality <100K, high-cardinality field block, net savings)
- **Dashboard integration**: Automatic — clones original Kibana visualizations, rewires to metrics index
- **Backfill**: Not supported — ES transforms are forward-only
- **Verification**: Side-by-side log aggregation vs. metrics query comparison

---

## Feature Comparison

| Capability | Datadog | Logs2Metrics |
|---|---|---|
| Metric types | Count, Distribution | Count, Sum, Avg, Distribution |
| Aggregation granularity | Fixed 10s | Configurable (10s–1h) |
| Group-by dimensions | Any log attribute | Any ES keyword field |
| Filter query | Log search syntax | ES query DSL |
| Processing latency | Real-time (at ingest) | Near-real-time (transform frequency + sync delay) |
| Historical backfill | No | No |
| Retention | 15 months | Configurable via ILM (default 450 days) |
| Cardinality guardrails | Implicit (billing) | Explicit (4 pre-creation checks) |
| Cost estimation | Post-hoc via billing page | Upfront log-vs-metric comparison |
| Dashboard analysis | None — user decides manually | Automated scoring (0-95) with 6 signals |
| Visualization cloning | None — user builds new widgets | Automatic — preserves chart type, axes, legend |
| Side-by-side verification | Not available | Built-in comparison |
| Guided pipeline UX | Simple form | 6-step walkthrough with previews |
| Rule lifecycle | Active / Deleted | Draft / Active / Paused / Error |
| API | Full REST | Full REST |
| Multi-backend | Datadog only | Abstract interface (ES now, Prometheus planned) |

---

## Where Logs2Metrics Leads

These are genuine differentiators over Datadog's approach:

### 1. Automated Dashboard Analysis & Scoring
Datadog requires the user to *know* which logs should become metrics. Logs2Metrics analyzes existing Kibana dashboards, parses each panel's aggregation structure, and scores suitability with 6 weighted signals (date_histogram, numeric aggs, no raw docs, aggregatable dims, lookback, auto-refresh). The system recommends what to convert — the user doesn't need to reverse-engineer their own dashboards.

### 2. Automatic Visualization Cloning
When Datadog users create log-based metrics, they must manually build new dashboard widgets. Logs2Metrics clones the original Kibana visualization (preserving chart type, axes, legend, colors, date_histogram + terms aggs) and rewires it to the metrics index. Same charts, zero manual work.

### 3. Side-by-Side Verification
Logs2Metrics runs the original log aggregation query and the metrics query in parallel, showing results side-by-side with doc counts, query times, and reduction percentages. Users can verify correctness before trusting pre-computed data. Datadog has no equivalent.

### 4. Explicit Cost Guardrails
Logs2Metrics blocks conversions that would increase cost (metric storage > log storage savings) before any resources are provisioned. Datadog relies on billing as a retroactive signal.

### 5. Richer Aggregation Options
Logs2Metrics supports Sum and Avg as first-class compute types alongside Count and Distribution, with configurable time bucket granularity. Datadog locks granularity to 10 seconds.

---

## Identified Gaps & Disposition

| Gap | Datadog Has | Our Position |
|---|---|---|
| **Alerting & Monitors** | Native metric monitors, anomaly detection | Not our goal. Kibana has its own alerting. Future Prometheus backend will integrate with Grafana alerting stack. This project transforms logs into metrics — it's not an observability platform. |
| **SLO Tracking** | Native SLO objects on log-based metrics | Same as alerting — covered by the dashboard/alerting platform (Kibana or Grafana), not by the transform engine. |
| **RBAC / Permissions** | Granular role-based access | Mediated by Kibana user/password authentication on the connection. The portal inherits Kibana's access controls. |
| **Tagging & Metadata** | Rich tag system for metric discovery | Will become relevant when the Prometheus/Grafana backend is implemented — Prometheus labels are the natural equivalent. Not needed for the ES/Kibana phase. |
| **Real-time Processing** | At-ingest, zero delay | Not a concern for operational dashboards. Transform frequency + sync delay (typically under 2 minutes) is acceptable for the on-prem use case. |
| **Metric Discovery / Catalog** | Metric Summary page, cardinality management | Not our goal. The project converts log aggregations into metrics. Metric governance is the platform's responsibility (Kibana or Grafana). |
| **Pre-Ingestion Pipeline** | Observability Pipelines (edge processing) | Not a concern. We want logs to remain in ES for flexibility and debugging. Customers who want pre-ingestion processing can use OpenTelemetry Collector or explicit in-app metric creation. |

---

## Summary

Logs2Metrics and Datadog solve the same core problem — converting repeated log aggregations into pre-computed metrics — but from opposite directions:

- **Datadog** says: *"Tell us which logs to convert, and we'll generate the metrics."* The user does the analysis; Datadog does the plumbing.
- **Logs2Metrics** says: *"Point us at your dashboards, and we'll figure out what to convert, prove it works, and build the new dashboards for you."* The system does the analysis AND the plumbing.

The gaps Datadog has over Logs2Metrics (alerting, SLOs, RBAC, metric catalog) are all **platform concerns** that belong to the observability stack (Kibana/Grafana), not to the transform engine. Logs2Metrics deliberately stays in its lane: analyze dashboards, validate costs, provision transforms, clone visualizations.

The planned Prometheus backend will naturally close the tagging gap (Prometheus labels) and unlock the Grafana ecosystem (alerting, SLOs, dashboards) without Logs2Metrics needing to build those capabilities itself.

---

## Sources

- [Datadog: Generate Metrics from Ingested Logs](https://docs.datadoghq.com/logs/log_configuration/logs_to_metrics/)
- [Datadog Blog: Log-Based Metrics](https://www.datadoghq.com/blog/log-based-metrics/)
- [Datadog Blog: Observability Pipelines Metrics from Logs](https://www.datadoghq.com/blog/observability-pipelines-generate-metrics-from-high-volume-logs/)
- [Datadog: Logs Metrics API](https://docs.datadoghq.com/api/latest/logs-metrics/)
- [Datadog: Custom Metrics Billing](https://docs.datadoghq.com/account_management/billing/custom_metrics/)
- [Datadog: Custom Metrics Governance](https://docs.datadoghq.com/metrics/guide/custom_metrics_governance/)
- [Datadog: Metrics without Limits](https://docs.datadoghq.com/metrics/metrics-without-limits/)
- [Datadog Pricing Caveats — SigNoz](https://signoz.io/blog/datadog-pricing/)
