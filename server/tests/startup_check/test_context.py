"""Unit tests for Startup Check user-context validation."""

import pytest

from services.startup_check.context import (
    ContextValidationError,
    UserContext,
    parse_user_context,
)


def test_empty_context_is_valid():
    ctx = parse_user_context(None)
    assert ctx.is_empty
    assert ctx.max_node_cpu_cores() is None
    assert ctx.max_node_memory_mb() is None


def test_full_context_parses():
    ctx = parse_user_context({
        "nodes": [{"name": "pool-a", "cpu_cores": 16, "memory_mb": 65536}],
        "service_memory_mb": {"worker": 150},
        "storage_classes": ["standard", "fast-ssd"],
        "default_storage_class": False,
        "cluster_domain": "cluster.local",
        "known_services": ["redis"],
        "known_configmaps": ["nginx-conf"],
        "known_secrets": ["api-keys"],
        "known_workloads": ["api"],
        "unknown_future_key": {"ignored": True},
    })
    assert not ctx.is_empty
    assert ctx.max_node_cpu_cores() == 16
    assert ctx.max_node_memory_mb() == 65536
    assert ctx.service_memory_mb == {"worker": 150.0}
    assert ctx.storage_classes == ["standard", "fast-ssd"]
    assert ctx.default_storage_class is False
    assert ctx.cluster_domain == "cluster.local"
    assert ctx.known_services == ["redis"]


@pytest.mark.parametrize("bad", [
    {"nodes": "nope"},
    {"nodes": [{"cpu_cores": 1}]},
    {"nodes": [{"cpu_cores": 0, "memory_mb": 1}]},
    {"nodes": [{"cpu_cores": 1, "memory_mb": -5}]},
    {"service_memory_mb": {"worker": "lots"}},
    {"service_memory_mb": {"worker": -1}},
    {"storage_classes": "standard"},
    {"storage_classes": [123]},
    {"default_storage_class": "maybe"},
    {"cluster_domain": 42},
    {"known_services": ["a", None]},
    12345,
])
def test_invalid_contexts_rejected(bad):
    with pytest.raises(ContextValidationError):
        parse_user_context(bad)


def test_blank_string_means_no_context():
    assert parse_user_context("").is_empty


def test_default_user_context_is_empty():
    assert UserContext().is_empty
