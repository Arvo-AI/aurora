"""Tests that exceptions from infrastructure layers never surface secret
material in HTTP response bodies, audit-event payloads, or log output.

Covers: the LLM safety judge (``check_command_safety``) and the DB
connection pool (``connect_to_db_as_admin``).  Input-rail and RBAC
decorator secret-leakage checks live in ``test_input_rail.py`` and
``test_rbac_decorators.py`` respectively, alongside the other exception
behaviour tests for those modules.
"""

import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))


_LLM_API_KEY_MARKER = "sk-proj-DO_NOT_LEAK_XYZ"
_DB_PASSWORD_MARKER = "db_password_DO_NOT_LEAK_XYZ"


def _llm_auth_exc() -> Exception:
    return RuntimeError(
        f"AuthenticationError: invalid API key 'sk-proj-DO_NOT_LEAK_XYZ'; "
        f"check that OPENAI_API_KEY={_LLM_API_KEY_MARKER} is correct"
    )


def _db_conn_exc() -> Exception:
    return RuntimeError(
        f"FATAL: password authentication failed for user 'aurora' "
        f"(password={_DB_PASSWORD_MARKER})"
    )


class TestCommandSafetyNoSecretLeak:
    """When ``_call_llm`` raises carrying an API key, the SafetyVerdict fields
    and log output must not contain it.  AI SDKs embed the rejected key in
    their AuthenticationError message text.
    """

    def test_llm_exception_verdict_fields_do_not_contain_secret(
        self, monkeypatch, caplog
    ):
        from utils.security import command_safety

        monkeypatch.setattr(command_safety, "config", MagicMock(enabled=True))
        monkeypatch.setattr(
            command_safety,
            "_get_latest_user_message",
            MagicMock(return_value="check disk"),
        )
        monkeypatch.setattr(
            command_safety,
            "_call_llm",
            MagicMock(side_effect=_llm_auth_exc()),
        )

        with caplog.at_level(logging.DEBUG):
            verdict = command_safety.check_command_safety("df -h")

        assert _LLM_API_KEY_MARKER not in verdict.thought
        assert _LLM_API_KEY_MARKER not in verdict.observation
        assert verdict.conclusion is True
        assert _LLM_API_KEY_MARKER not in caplog.text

        for record in caplog.records:
            assert _LLM_API_KEY_MARKER not in record.getMessage(), (
                f"API key found in log record: {record.getMessage()!r}"
            )


class TestDbConnectionNoSecretLeak:
    """When the connection pool raises carrying a DB password,
    ``connect_to_db_as_admin`` must not forward it to the caller or logs.

    ``db_adapters.py`` catches pool exceptions and re-raises a sanitised
    message, but the intermediate ``logger.error`` call is the leak risk
    being pinned here.
    """

    def test_pool_exception_not_logged_with_password(self, monkeypatch, caplog):
        import utils.db.db_adapters as adapters_mod

        fake_pool = MagicMock()
        fake_pool.getconn.side_effect = _db_conn_exc()

        monkeypatch.setattr(adapters_mod.db_pool, "_get_pool", MagicMock(return_value=fake_pool))

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(Exception):
                adapters_mod.connect_to_db_as_admin()

        assert _DB_PASSWORD_MARKER not in caplog.text
        for record in caplog.records:
            assert _DB_PASSWORD_MARKER not in record.getMessage(), (
                f"DB password found in log record: {record.getMessage()!r}"
            )
