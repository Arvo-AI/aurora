"""Readiness / liveness probe semantics in ``routes/health_routes.py``.

The kubelet gives the readiness probe a fixed timeout. The shared DB pool
used to make the probe wait the full pool timeout (5s) whenever every pooled
connection was checked out, so a busy pod failed readiness for a reason that
was not "Postgres is down", was pulled from the Service, and one probe later
was killed by liveness. These tests pin the new contract:

* readiness waits a bounded, short time for a pooled connection;
* an exhausted pool is reported as ``degraded`` with HTTP 200;
* a real Postgres or Redis failure is still HTTP 503;
* liveness touches no dependency at all.
"""

import contextlib
import sys
import types
from unittest.mock import MagicMock

import psycopg2
import psycopg2.pool
import pytest
from flask import Flask


@pytest.fixture
def health(monkeypatch):
    """Import ``routes.health_routes`` with ``celery_config`` stubbed."""
    celery_stub = types.ModuleType("celery_config")
    celery_stub.celery_app = MagicMock(name="celery_app")
    monkeypatch.setitem(sys.modules, "celery_config", celery_stub)
    monkeypatch.setenv("POSTGRES_USER", "aurora")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    sys.modules.pop("routes.health_routes", None)
    import routes.health_routes as module

    yield module
    sys.modules.pop("routes.health_routes", None)


@pytest.fixture
def client(health):
    app = Flask(__name__)
    app.register_blueprint(health.health_bp, url_prefix="/health")
    return app.test_client()


def _stub_pool(monkeypatch, *, raises=None, seen_waits=None):
    from utils.db import connection_pool as pool_module

    @contextlib.contextmanager
    def fake_get_connection(wait_timeout=None):
        if seen_waits is not None:
            seen_waits.append(wait_timeout)
        if raises is not None:
            raise raises
        conn = MagicMock(name="conn")
        conn.cursor.return_value.__enter__.return_value = MagicMock(name="cursor")
        yield conn

    monkeypatch.setattr(pool_module.db_pool, "get_connection", fake_get_connection)


def _stub_redis(monkeypatch, health, status="healthy"):
    monkeypatch.setattr(
        health, "check_redis_health", lambda timeout=None: {"status": status}
    )


def test_readiness_reports_busy_pool_as_degraded_200(monkeypatch, health, client):
    _stub_pool(monkeypatch, raises=psycopg2.pool.PoolError("connection pool exhausted"))
    _stub_redis(monkeypatch, health)

    resp = client.get("/health/readiness")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["status"] == "degraded"


def test_readiness_is_503_when_postgres_is_unreachable(monkeypatch, health, client):
    _stub_pool(monkeypatch, raises=psycopg2.OperationalError("could not connect"))
    _stub_redis(monkeypatch, health)

    resp = client.get("/health/readiness")

    assert resp.status_code == 503
    assert resp.get_json()["status"] == "not_ready"
    assert resp.get_json()["checks"]["database"]["status"] == "unhealthy"


def test_readiness_is_503_when_redis_is_down(monkeypatch, health, client):
    _stub_pool(monkeypatch)
    _stub_redis(monkeypatch, health, status="unhealthy")

    resp = client.get("/health/readiness")

    assert resp.status_code == 503


def test_readiness_is_200_ready_when_everything_answers(monkeypatch, health, client):
    _stub_pool(monkeypatch)
    _stub_redis(monkeypatch, health)

    resp = client.get("/health/readiness")

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ready"


def test_readiness_waits_only_the_probe_budget_for_a_connection(monkeypatch, health, client):
    seen = []
    _stub_pool(monkeypatch, seen_waits=seen)
    _stub_redis(monkeypatch, health)

    client.get("/health/readiness")

    assert seen == [health._PROBE_POOL_WAIT_SECONDS]
    assert health._PROBE_POOL_WAIT_SECONDS < 5.0  # the pool's own default wait


def test_liveness_touches_no_dependency(monkeypatch, health, client):
    from utils.db import connection_pool as pool_module

    def explode(*args, **kwargs):
        raise AssertionError("liveness must not touch the DB pool")

    monkeypatch.setattr(pool_module.db_pool, "get_connection", explode)
    monkeypatch.setattr(health, "check_redis_health", explode)

    resp = client.get("/health/liveness")

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "alive"}


def test_redis_check_applies_socket_timeouts_and_closes_client(monkeypatch, health):
    captured = {}
    fake_client = MagicMock(name="redis_client")

    def fake_from_url(url, **kwargs):
        captured.update(kwargs)
        return fake_client

    monkeypatch.setattr(health.redis, "from_url", fake_from_url)
    import utils.cache.redis_client as redis_client_module

    monkeypatch.setattr(redis_client_module, "get_redis_ssl_kwargs", lambda: {})

    result = health.check_redis_health(timeout=2.0)

    assert result["status"] == "healthy"
    assert captured["socket_timeout"] == 2.0
    assert captured["socket_connect_timeout"] == 2.0
    fake_client.ping.assert_called_once()
    fake_client.close.assert_called_once()
