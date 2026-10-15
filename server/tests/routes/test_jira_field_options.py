"""Field catalogs for the Jira RCA picker collapse to unique names."""

from connectors.jira_connector.client import JiraClient, _page_values
from routes.jira.field_options import status_names, unique_names


def test_unique_names_dedupes_and_sorts():
    assert unique_names([
        {"name": "High"},
        {"name": " high "},
        "Low",
        {"name": ""},
        "x" * 81,
        None,
    ]) == ["High", "Low"]


def test_status_names_flatten_issue_types():
    payload = [
        {"name": "Bug", "statuses": [{"name": "Done"}, {"name": "To Do"}]},
        {"name": "Task", "statuses": [{"name": "done"}, {"name": "In Progress"}]},
    ]
    assert status_names(payload) == ["Done", "In Progress", "To Do"]


def test_page_values_accepts_list_or_wrapped_page():
    assert _page_values([{"name": "High"}]) == [{"name": "High"}]
    assert _page_values({"values": ["a"], "isLast": True}) == ["a"]
    assert _page_values({}) == []


def test_list_labels_reports_truncation(monkeypatch):
    client = JiraClient("https://jira.example.test", "token", auth_type="pat")
    pages = iter([
        {"values": ["a", "b"], "isLast": False},
        {"values": ["c"], "isLast": False},
    ])
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: next(pages))

    labels, truncated = client.list_labels(limit=2)

    assert labels == ["a", "b"]
    assert truncated is True


def test_list_priorities_filters_to_the_project_scheme(monkeypatch):
    client = JiraClient("https://jira.example.test", "token", auth_type="oauth", cloud_id="cloud")
    calls = []

    def request(method, path, params=None, json_body=None):
        calls.append((path, params))
        if path == "/project/OPS":
            return {"id": "10001", "key": "OPS"}
        return {"values": [{"name": "High"}], "isLast": True}

    monkeypatch.setattr(client, "_request", request)

    assert client.list_priorities("OPS") == [{"name": "High"}]
    assert calls[1][1]["projectId"] == "10001"


def test_list_priorities_falls_back_when_search_rejects_the_token(monkeypatch):
    client = JiraClient("https://jira.example.test", "token", auth_type="pat")

    def request(method, path, params=None, json_body=None):
        if path == "/priority/search":
            raise _http_error(401)
        assert path == "/priority"
        return [{"name": "High"}]

    monkeypatch.setattr(client, "_request", request)
    assert client.list_priorities() == [{"name": "High"}]


def test_list_priorities_falls_back_when_search_is_missing(monkeypatch):
    client = JiraClient("https://jira.example.test", "token", auth_type="pat")

    def request(method, path, params=None, json_body=None):
        if path == "/priority/search":
            raise _http_error(404)
        assert path == "/priority"
        return [{"name": "High"}]

    monkeypatch.setattr(client, "_request", request)
    assert client.list_priorities() == [{"name": "High"}]


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int):
    import requests

    error = requests.HTTPError("missing")
    error.response = _Response(status)
    return error
