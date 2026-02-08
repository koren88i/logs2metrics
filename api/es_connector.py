"""Elasticsearch read-only connector.

Provides index metadata, mappings, cardinality, and stats.
"""

from elasticsearch import Elasticsearch

from config import ES_URL
from connector_models import (
    FieldCardinality,
    FieldMapping,
    IndexInfo,
    IndexMapping,
    IndexStats,
)

es = Elasticsearch(ES_URL)


def _format_bytes(n: int) -> str:
    """Convert bytes to human-readable string."""
    for unit in ("b", "kb", "mb", "gb", "tb"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "b" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}pb"


def list_indices(pattern: str = "*") -> list[IndexInfo]:
    """Return index names, doc counts, and store sizes matching a pattern."""
    rows = es.cat.indices(index=pattern, format="json", h="index,docs.count,store.size", bytes="b", s="index")
    results = []
    for row in rows:
        name = row["index"]
        if name.startswith("."):
            continue
        size_bytes = int(row.get("store.size") or 0)
        results.append(IndexInfo(
            name=name,
            doc_count=int(row.get("docs.count") or 0),
            store_size_bytes=size_bytes,
            store_size_human=_format_bytes(size_bytes),
        ))
    return results


def get_mapping(index: str) -> IndexMapping:
    """Return field names and types for an index."""
    raw = es.indices.get_mapping(index=index)
    properties = raw[index]["mappings"].get("properties", {})
    fields = []
    for field_name, field_def in sorted(properties.items()):
        field_type = field_def.get("type", "object")
        aggregatable = field_type != "text"
        fields.append(FieldMapping(
            name=field_name,
            type=field_type,
            aggregatable=aggregatable,
        ))
    return IndexMapping(index=index, fields=fields)


def get_field_cardinality(index: str, field: str) -> FieldCardinality:
    """Return approximate distinct count for a field in an index."""
    result = es.search(
        index=index,
        size=0,
        aggs={"cardinality": {"cardinality": {"field": field}}},
    )
    value = result["aggregations"]["cardinality"]["value"]
    return FieldCardinality(index=index, field=field, cardinality=value)


def get_index_stats(index: str) -> IndexStats:
    """Return doc count, size, and query rate for an index."""
    raw = es.indices.stats(index=index)
    total = raw["_all"]["total"]
    docs_count = total["docs"]["count"]
    store_bytes = total["store"]["size_in_bytes"]
    query_total = total.get("search", {}).get("query_total", 0)
    query_time = total.get("search", {}).get("query_time_in_millis", 0)
    return IndexStats(
        index=index,
        doc_count=docs_count,
        store_size_bytes=store_bytes,
        store_size_human=_format_bytes(store_bytes),
        query_total=query_total,
        query_time_ms=query_time,
    )
