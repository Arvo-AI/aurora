"""Whether a write points SSH at a different host, key, or user."""

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

# The linter job does not install paramiko. The route imports it through
# ssh_utils, and this file never opens a real SSH session.
try:
    __import__("paramiko")
except ModuleNotFoundError:
    sys.modules["paramiko"] = MagicMock()

import routes.vms.manual_vms_routes as vms


def test_unchanged_ssh_target_does_not_count_as_a_change():
    stored = {
        "ip_address": "203.0.113.10",
        "port": 22,
        "ssh_jump_command": None,
        "ssh_key_id": 7,
        "ssh_username": "ubuntu",
    }
    # Save resubmits blanks and strings for values that are already stored
    proposed = {
        "ip_address": "203.0.113.10",
        "port": "22",
        "ssh_jump_command": "  ",
        "ssh_key_id": 7,
        "ssh_username": "ubuntu",
    }
    assert vms.connection_fields_changed(stored, proposed) is False


def test_a_different_jump_host_invalidates_the_check():
    stored = {"ssh_jump_command": None, "ssh_key_id": 7}
    assert vms.connection_fields_changed(
        stored, {"ssh_jump_command": "ssh -J bastion"}
    ) is True


def test_omitted_fields_do_not_invalidate_the_check():
    stored = {"ip_address": "203.0.113.10", "ssh_key_id": 7}
    assert vms.connection_fields_changed(stored, {}) is False


def test_a_blank_saved_user_adopts_the_tested_user():
    stored = {"ip_address": "203.0.113.10", "port": 22, "ssh_username": None}
    tested = {"ip_address": "203.0.113.10", "port": 22, "ssh_username": "ubuntu"}
    compared, fill = vms.target_for_ready_check(stored, tested)
    assert fill is True
    assert vms.connection_fields_changed(compared, tested) is False


def test_a_different_saved_user_is_not_replaced():
    stored = {"ssh_username": "ubuntu"}
    tested = {"ssh_username": "root"}
    compared, fill = vms.target_for_ready_check(stored, tested)
    assert fill is False
    assert vms.connection_fields_changed(compared, tested) is True
