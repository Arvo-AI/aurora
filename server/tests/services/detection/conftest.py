import pytest


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """No test here reaches a real Redis, or one another test left faked. A test
    about pacing puts its own in place."""
    from connectors.slack_connector import read_limit

    def _none():
        raise ConnectionError("no Redis in this test")

    monkeypatch.setattr(read_limit, "get_redis_client", _none)
