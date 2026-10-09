"""Platform policy memories: the Slack identity/strings are unchanged (pinned by
hash), the force-injection lookup behaves as before for every existing source,
a second platform plugs in and joins the single routing agent, and seeding is
idempotent and lands in the caller's org."""

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from services.memory import (
    PLATFORM_MEMORY_IDENTITIES,
    PROTECTED_ENTRIES,
    SLACK_MEMORY_CATEGORY,
    SLACK_MEMORY_TITLE,
    PlatformMemoryIdentity,
    policy_entries_for_source,
)
from services.memory import platform_memory as pm
from services.memory import slack_memory

# sha256 of the literals in the pre-refactor services/memory/slack_memory.py.
_SLACK_DESCRIPTION_SHA = "b418a976cbc1bcaf3078d4f2b02bf070699d33e0e8f24b555528f7542b021bec"
_SLACK_CONTENT_SHA = "ff91ce07cf94b4b1cf1911f2c6f2ece8f3e9854d4a6ab8a4ccdb08b7a91673ea"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_slack_identity_and_strings_unchanged():
    ident = PLATFORM_MEMORY_IDENTITIES["slack"]
    assert ident.key == (SLACK_MEMORY_CATEGORY, SLACK_MEMORY_TITLE) == ("context", "Slack")
    assert ("context", "Slack") in PROTECTED_ENTRIES
    assert ("context", "Microsoft Teams") in PROTECTED_ENTRIES
    # slack_memory owns the strings the spec is built from; both must be the pre-refactor text.
    assert _sha(slack_memory.SLACK_MEMORY_DESCRIPTION) == _SLACK_DESCRIPTION_SHA
    assert _sha(slack_memory.SLACK_MEMORY_DEFAULT_CONTENT) == _SLACK_CONTENT_SHA


@pytest.mark.parametrize("source,expected", [
    ("slack", [("context", "Slack")]),
    ("SLACK", [("context", "Slack")]),
    ("team_routing", [("context", "Slack"), ("context", "Microsoft Teams")]),
    ("slack_button", []),
    ("google_chat", []),
    ("grafana", []),
    ("chat", []),
    ("", []),
    (None, []),
])
def test_policy_entries_for_source_matches_pre_refactor_behaviour(source, expected):
    assert policy_entries_for_source(source) == expected


def test_a_second_platform_gets_its_own_memory_and_joins_the_routing_agent():
    fake = PlatformMemoryIdentity("fake", "context", "Fake Chat", frozenset({"fake"}))
    with patch.dict(PLATFORM_MEMORY_IDENTITIES, {"fake": fake}):
        assert policy_entries_for_source("fake") == [("context", "Fake Chat")]
        assert policy_entries_for_source("slack") == [("context", "Slack")]
        # One routing agent decides for every platform, so it gets every policy.
        assert policy_entries_for_source("team_routing") == [
            ("context", "Slack"),
            ("context", "Microsoft Teams"),
            ("context", "Fake Chat"),
        ]


def _db(existing_row=None, insert_row=("artifact-1",)):
    conn = MagicMock()
    cur = MagicMock()
    # First fetchone = existence check, second = RETURNING id.
    cur.fetchone.side_effect = [existing_row, insert_row]
    conn.cursor.return_value.__enter__.return_value = cur
    pool = MagicMock()
    pool.get_admin_connection.return_value.__enter__.return_value = conn
    return pool, cur, conn


def test_seed_creates_entry_and_version_in_the_callers_org():
    pool, cur, conn = _db(existing_row=None)
    with patch.object(pm, "db_pool", pool), \
         patch.object(pm, "set_rls_context", return_value="org-1") as rls, \
         patch.object(pm, "create_version") as cv:
        assert pm.seed_platform_memory("u1", "slack", org_id="org-1") is True
    # org_id from the request flows into the RLS SET (seeding into another org
    # than the one the caller reads back was the #674 bug).
    assert rls.call_args.kwargs["org_id"] == "org-1"
    insert_sql, insert_params = cur.execute.call_args_list[1].args
    assert "INSERT INTO artifacts" in insert_sql
    assert "ON CONFLICT (org_id, category, title) DO NOTHING" in insert_sql
    assert insert_params == ("org-1", "u1", "Slack", slack_memory.SLACK_MEMORY_DEFAULT_CONTENT,
                             "context", slack_memory.SLACK_MEMORY_DESCRIPTION)
    assert cv.call_args.args[:4] == (cur, "artifact-1", "org-1", "u1")
    assert cv.call_args.kwargs.get("source") == "agent"
    conn.commit.assert_called()


def test_seed_never_overwrites_an_existing_entry():
    pool, cur, _conn = _db(existing_row=("existing",))
    with patch.object(pm, "db_pool", pool), \
         patch.object(pm, "set_rls_context", return_value="org-1"), \
         patch.object(pm, "create_version") as cv:
        assert pm.seed_platform_memory("u1", "slack") is False
    assert len(cur.execute.call_args_list) == 1  # only the existence check
    cv.assert_not_called()


def test_slack_shim_seed_forwards_org_id():
    with patch.object(pm, "seed_platform_memory", return_value=True) as seed:
        assert slack_memory.seed_slack_memory("u1", org_id="org-9") is True
    seed.assert_called_once_with("u1", "slack", org_id="org-9")
