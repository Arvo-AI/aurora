"""Filter-picker catalog: catalog tables first, recent-incident values as the fallback."""

from unittest.mock import MagicMock

from routes.servicenow.field_options import build_field_options
from routes.servicenow.servicenow_client import ServiceNowAPIError, ServiceNowClient


def test_catalog_tables_are_used_when_readable():
    client = MagicMock()
    client.list_assignment_groups.return_value = ["Network", "Platform"]
    client.list_categories.return_value = ["Software", "Hardware"]
    out = build_field_options(client)
    assert out["assignmentGroups"] == ["Network", "Platform"]
    assert out["categories"] == ["Software", "Hardware"]
    assert out["states"][0] == {"value": "1", "label": "New"}
    assert out["errors"] == []
    client.distinct_incident_values.assert_not_called()


def test_falls_back_to_values_on_recent_incidents_when_the_table_is_refused():
    client = MagicMock()
    client.list_assignment_groups.side_effect = ServiceNowAPIError("forbidden", 403)
    client.list_categories.return_value = ["Software"]
    client.distinct_incident_values.return_value = ["Service Desk"]
    out = build_field_options(client)
    assert out["assignmentGroups"] == ["Service Desk"]
    client.distinct_incident_values.assert_called_once_with("assignment_group")
    assert out["errors"] == []


def test_a_catalog_that_fails_twice_is_reported_not_raised():
    client = MagicMock()
    client.list_assignment_groups.side_effect = ServiceNowAPIError("down", 503)
    client.distinct_incident_values.side_effect = ServiceNowAPIError("down", 503)
    client.list_categories.return_value = []
    out = build_field_options(client)
    assert out["assignmentGroups"] == []
    assert out["errors"] == ["assignmentGroups"]


def test_names_dedupe_and_unwrap_reference_links():
    rows = [{"name": "Network"}, {"name": " network "}, {"name": {"display_value": "Platform"}}, {"name": ""}, "junk"]
    assert ServiceNowClient._names(rows, "name") == ["Network", "Platform"]
