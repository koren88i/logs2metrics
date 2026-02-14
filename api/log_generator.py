"""Log generation for debug/demo purposes.

Generates realistic synthetic log documents directly into the connected
Elasticsearch instance.  Previously this was a separate Docker service;
now it lives inside the API and uses the caller-supplied ES client.
"""

import random
import time
from datetime import datetime, timedelta, timezone

from elasticsearch import Elasticsearch, helpers

ES_INDEX = "app-logs"

SERVICES = ["auth-service", "api-gateway", "order-service", "payment-service", "user-service"]
ENDPOINTS = ["/api/login", "/api/users", "/api/orders", "/api/payments", "/api/health", "/api/products"]
TENANTS = ["acme-corp", "globex", "initech", "umbrella", "wayne-ent"]
STATUS_CODES = [200, 200, 200, 200, 200, 201, 204, 301, 400, 401, 403, 404, 500, 502, 503]
LEVELS = ["INFO", "INFO", "INFO", "INFO", "WARN", "ERROR"]

INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "timestamp": {"type": "date"},
            "service": {"type": "keyword"},
            "endpoint": {"type": "keyword"},
            "status_code": {"type": "integer"},
            "response_time_ms": {"type": "float"},
            "tenant": {"type": "keyword"},
            "level": {"type": "keyword"},
            "message": {"type": "text"},
        }
    }
}


def _generate_log_entry(ts: datetime) -> dict:
    status = random.choice(STATUS_CODES)
    level = "ERROR" if status >= 500 else ("WARN" if status >= 400 else random.choice(LEVELS))
    base_latency = random.uniform(5, 50)
    latency = base_latency * (random.uniform(3, 20) if status >= 500 else 1)
    return {
        "timestamp": ts.isoformat(),
        "service": random.choice(SERVICES),
        "endpoint": random.choice(ENDPOINTS),
        "status_code": status,
        "response_time_ms": round(latency, 2),
        "tenant": random.choice(TENANTS),
        "level": level,
        "message": f"{level}: {random.choice(ENDPOINTS)} responded {status}",
    }


def _build_log_docs(count: int, max_age_seconds: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    docs = []
    for _ in range(count):
        ts = now - timedelta(seconds=random.randint(0, max_age_seconds))
        doc = _generate_log_entry(ts)
        docs.append({"_index": ES_INDEX, "_source": doc})
    return docs


def _ensure_index(es: Elasticsearch):
    if not es.indices.exists(index=ES_INDEX):
        es.indices.create(index=ES_INDEX, body=INDEX_MAPPING)


def _bulk_ingest(es: Elasticsearch, actions: list[dict]) -> dict:
    start = time.time()
    _ensure_index(es)
    success, errors = helpers.bulk(es, actions, raise_on_error=False)
    duration = round(time.time() - start, 2)
    return {
        "count_requested": len(actions),
        "count_ingested": success,
        "errors": len(errors) if isinstance(errors, list) else 0,
        "duration_seconds": duration,
        "index": ES_INDEX,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def generate(es: Elasticsearch, count: int = 1000) -> dict:
    """Generate logs spread across the last 24 hours."""
    actions = _build_log_docs(count, max_age_seconds=86400)
    return _bulk_ingest(es, actions)


def generate_recent(es: Elasticsearch, count: int = 50) -> dict:
    """Generate logs timestamped at exactly now (for live injection into open buckets)."""
    actions = _build_log_docs(count, max_age_seconds=0)
    result = _bulk_ingest(es, actions)
    result["recent"] = True
    result["description"] = f"{result['count_ingested']} logs timestamped at now."
    return result


def generate_toy(es: Elasticsearch) -> dict:
    """Generate a small, predictable toy dataset for end-to-end testing.

    Creates 10 identical logs within the same minute:
      - Same service (auth-service), endpoint (/api/login), tenant (acme-corp)
      - All status 200, level INFO, response_time_ms 42.0

    This should compress to exactly 1 metric point with count=10
    when grouped by (service, endpoint) at a 1m bucket.
    """
    start = time.time()
    _ensure_index(es)

    now = datetime.now(timezone.utc)
    base_ts = now.replace(second=0, microsecond=0) - timedelta(minutes=5)

    actions = []
    for i in range(10):
        doc = {
            "timestamp": (base_ts + timedelta(seconds=i * 5)).isoformat(),
            "service": "auth-service",
            "endpoint": "/api/login",
            "status_code": 200,
            "response_time_ms": 42.0,
            "tenant": "acme-corp",
            "level": "INFO",
            "message": "INFO: /api/login responded 200",
        }
        actions.append({"_index": ES_INDEX, "_source": doc})

    success, errors = helpers.bulk(es, actions, raise_on_error=False)
    duration = round(time.time() - start, 2)

    return {
        "count_requested": 10,
        "count_ingested": success,
        "errors": len(errors) if isinstance(errors, list) else 0,
        "duration_seconds": duration,
        "index": ES_INDEX,
        "generated_at": now.isoformat(),
        "toy_scenario": True,
        "description": "10 identical logs (auth-service, /api/login, acme-corp) within 1 minute. Expect 1 metric point with count=10.",
    }


def delete_logs(es: Elasticsearch) -> dict:
    """Delete all documents from the log index."""
    if not es.indices.exists(index=ES_INDEX):
        return {"deleted": 0, "index": ES_INDEX}
    result = es.delete_by_query(
        index=ES_INDEX,
        body={"query": {"match_all": {}}},
        refresh=True,
    )
    return {"deleted": result["deleted"], "index": ES_INDEX}
