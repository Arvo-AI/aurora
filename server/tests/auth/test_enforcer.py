"""Tests the Casbin RBAC enforcer behind ``@require_permission`` on every
authenticated route. Pins single-role-per-(user, org) replacement (so a
demotion can't leave the old role in place -- silent privilege
escalation) and the per-org domain matcher (so policies scoped to one
org can't leak across tenants).
"""

from unittest.mock import MagicMock

import pytest

from utils.auth import enforcer as enforcer_module
from utils.auth.enforcer import assign_role_to_user


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_enforcer(monkeypatch):
    """Return a MagicMock standing in for ``get_enforcer()``."""
    fake = MagicMock(name="casbin_enforcer")
    fake.get_roles_for_user_in_domain.return_value = []
    monkeypatch.setattr(enforcer_module, "get_enforcer", lambda: fake)
    monkeypatch.setattr(enforcer_module, "get_redis_client", lambda: None)
    return fake


# ---------------------------------------------------------------------------
# Replace-not-add semantics
# ---------------------------------------------------------------------------


class TestRoleReplacement:
    """Every old role must be removed before the new one is added."""

    def test_editor_to_viewer_removes_editor_then_adds_viewer(self, fake_enforcer):
        """``[editor] -> viewer``: one remove, one add."""
        fake_enforcer.get_roles_for_user_in_domain.return_value = ["editor"]

        assign_role_to_user("u-1", "viewer", "org-7")

        fake_enforcer.remove_grouping_policy.assert_called_once_with(
            "u-1", "editor", "org-7",
        )
        fake_enforcer.add_grouping_policy.assert_called_once_with(
            "u-1", "viewer", "org-7",
        )

    def test_admin_and_editor_to_viewer_removes_both(self, fake_enforcer):
        """``[admin, editor] -> viewer``: every stale role is dropped."""
        fake_enforcer.get_roles_for_user_in_domain.return_value = ["admin", "editor"]

        assign_role_to_user("u-1", "viewer", "org-7")

        removed = {
            call.args[1]
            for call in fake_enforcer.remove_grouping_policy.call_args_list
        }
        assert removed == {"admin", "editor"}
        assert fake_enforcer.remove_grouping_policy.call_count == 2
        fake_enforcer.add_grouping_policy.assert_called_once_with(
            "u-1", "viewer", "org-7",
        )

    def test_viewer_to_viewer_is_idempotent(self, fake_enforcer):
        """Re-assigning the same role: no remove, no re-add."""
        fake_enforcer.get_roles_for_user_in_domain.return_value = ["viewer"]

        assign_role_to_user("u-1", "viewer", "org-7")

        fake_enforcer.remove_grouping_policy.assert_not_called()
        fake_enforcer.add_grouping_policy.assert_not_called()

    def test_no_existing_roles_just_adds_new_role(self, fake_enforcer):
        """Empty role set: nothing to remove, only the new role is added."""
        fake_enforcer.get_roles_for_user_in_domain.return_value = []

        assign_role_to_user("u-1", "editor", "org-7")

        fake_enforcer.remove_grouping_policy.assert_not_called()
        fake_enforcer.add_grouping_policy.assert_called_once_with(
            "u-1", "editor", "org-7",
        )

    def test_promote_drops_other_roles_and_adds_target(self, fake_enforcer):
        """``[viewer, admin] -> editor``: viewer + admin removed, editor added."""
        fake_enforcer.get_roles_for_user_in_domain.return_value = ["viewer", "admin"]

        assign_role_to_user("u-1", "editor", "org-7")

        removed = {
            call.args[1]
            for call in fake_enforcer.remove_grouping_policy.call_args_list
        }
        assert removed == {"viewer", "admin"}
        fake_enforcer.add_grouping_policy.assert_called_once_with(
            "u-1", "editor", "org-7",
        )

    def test_existing_target_role_kept_other_role_removed(self, fake_enforcer):
        """``[viewer, editor] -> editor``: viewer removed, editor not re-added."""
        fake_enforcer.get_roles_for_user_in_domain.return_value = ["viewer", "editor"]

        assign_role_to_user("u-1", "editor", "org-7")

        fake_enforcer.remove_grouping_policy.assert_called_once_with(
            "u-1", "viewer", "org-7",
        )
        fake_enforcer.add_grouping_policy.assert_not_called()

    def test_promote_to_admin_drops_lower_roles_and_adds_admin(self, fake_enforcer):
        """``[viewer, editor] -> admin``: lower roles removed, admin added."""
        fake_enforcer.get_roles_for_user_in_domain.return_value = ["viewer", "editor"]

        assign_role_to_user("u-1", "admin", "org-7")

        removed = {
            call.args[1]
            for call in fake_enforcer.remove_grouping_policy.call_args_list
        }
        assert removed == {"viewer", "editor"}
        assert fake_enforcer.remove_grouping_policy.call_count == 2
        fake_enforcer.add_grouping_policy.assert_called_once_with(
            "u-1", "admin", "org-7",
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPolicyPersistence:
    """``save_policy`` + ``load_policy`` run on every assign call."""

    def test_save_and_load_invoked_after_role_replacement(self, fake_enforcer):
        fake_enforcer.get_roles_for_user_in_domain.return_value = ["editor"]

        assign_role_to_user("u-1", "viewer", "org-7")

        fake_enforcer.save_policy.assert_called_once()
        fake_enforcer.load_policy.assert_called_once()

    def test_save_and_load_invoked_even_when_assignment_is_noop(self, fake_enforcer):
        """Idempotent re-assigns still flush: another worker may have written."""
        fake_enforcer.get_roles_for_user_in_domain.return_value = ["viewer"]

        assign_role_to_user("u-1", "viewer", "org-7")

        fake_enforcer.save_policy.assert_called_once()
        fake_enforcer.load_policy.assert_called_once()


# ---------------------------------------------------------------------------
# Domain (org) matcher
# ---------------------------------------------------------------------------


class TestDomainMatch:
    """``_domain_match`` enforces org isolation; ``*`` is the only wildcard."""

    @pytest.fixture
    def domain_match(self, monkeypatch):
        """Run ``get_enforcer`` and return the domain matcher closure it registers."""
        monkeypatch.setattr(enforcer_module, "_enforcer", None)

        fake = MagicMock(name="casbin_enforcer")
        fake.get_policy.return_value = []
        monkeypatch.setattr(
            enforcer_module.casbin,
            "Enforcer",
            MagicMock(return_value=fake),
        )

        enforcer_module.get_enforcer()

        g_calls = [
            call
            for call in fake.add_named_domain_matching_func.call_args_list
            if call.args and call.args[0] == "g"
        ]
        assert g_calls, "domain matcher was never registered for 'g'"
        return g_calls[-1].args[1]

    def test_exact_org_match(self, domain_match):
        assert domain_match("org-x", "org-x") is True

    def test_wildcard_in_policy_matches_any_org(self, domain_match):
        """``key2 == "*"`` -> match. Built-in role policies rely on this."""
        assert domain_match("org-x", "*") is True

    def test_two_concrete_orgs_do_not_match(self, domain_match):
        """``org-x`` must not inherit policies scoped to ``org-y``."""
        assert domain_match("org-x", "org-y") is False

    def test_wildcard_only_matches_when_in_policy_position(self, domain_match):
        """Asymmetric: a request with org=``*`` must NOT match every policy."""
        assert domain_match("*", "org-x") is False

    def test_empty_strings_do_not_match_anything(self, domain_match):
        assert domain_match("", "org-x") is False
        assert domain_match("org-x", "") is False
