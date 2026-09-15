"""Elastic Cloud (Elasticsearch + Kibana) connector."""

from .client import (  # noqa: F401
    ElasticAPIError,
    ElasticClient,
    build_log_query,
    compact_hits,
    normalize_api_key,
    normalize_index_pattern,
    normalize_url,
    parse_cloud_id,
)
