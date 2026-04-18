import os
import sys
import json
import pytest
from unittest.mock import MagicMock, patch

@pytest.fixture
def env_and_path(monkeypatch):
    """
    Pytest fixture to mock environment variables and adjust sys.path
    so that the server directory is accessible during testing.
    """
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("DEV_SECURITYHUB_API_KEY", "super-secret")
    server_path = os.path.join(os.path.dirname(__file__), "server")
    monkeypatch.syspath_prepend(server_path)

def test_route(env_and_path):
    """
    Test the Security Hub webhook POST route.
    Validates API key behavior and ensuring processing tasks are enqueued.
    """
    from flask import Flask
    from routes.aws.securityhub_routes import securityhub_bp
    
    app = Flask(__name__)
    app.register_blueprint(securityhub_bp, url_prefix="/aws/securityhub")

    with patch("routes.aws.securityhub_routes.db_pool") as mock_db_pool, \
         patch("routes.aws.securityhub_routes.process_securityhub_finding.delay") as mock_delay:
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_db_pool.get_admin_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        # Simulate DB returning empty for API key so it relies on DEV_SECURITYHUB_API_KEY
        mock_cursor.fetchone.return_value = None
        
        with app.test_client() as client:
            payload = {
                "source": "aws.securityhub",
                "detail": {
                    "findings": [{"Id": "TEST-1234", "Title": "Malware Found", "Severity": {"Label": "CRITICAL"}}]
                }
            }
            resp = client.post(
                "/aws/securityhub/webhook/TEST-ORG",
                json=payload,
                headers={"x-api-key": "super-secret"}
            )
            
            assert resp.status_code == 200
            assert "received" in resp.json and resp.json["received"] is True
            mock_delay.assert_called_once()

def test_task(env_and_path):
    """
    Test the Security Hub background task logic.
    Ensures that correct fields are extracted into SQL queries.
    """
    from routes.aws.tasks import process_securityhub_finding

    with patch("routes.aws.tasks.db_pool") as mock_db_pool:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_db_pool.get_admin_connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        payload = {
            "detail": {
                "findings": [{"Id": "TEST-1234", "Title": "Malware Found", "Severity": {"Label": "CRITICAL"}}]
            }
        }
        
        process_securityhub_finding(payload, "TEST-ORG")
        
        assert mock_cursor.execute.call_count > 0
        query, args = mock_cursor.execute.call_args[0]
        assert "aws_security_findings" in query
        assert "TEST-1234" in args

if __name__ == "__main__":
    pytest.main([__file__])

