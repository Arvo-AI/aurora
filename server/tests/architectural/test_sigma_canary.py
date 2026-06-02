"""Canary tests: benign commands must NEVER trigger a Sigma rule.

If any of these fail, the Sigma rule that matched is too broad for the
Aurora agent context and should be suppressed or the transpiler has a bug.
"""

import pytest

from utils.security.sigma_loader import load_sigma_rules
from utils.security.signature_match import SignatureVerdict


@pytest.fixture(scope="module")
def sigma_rules():
    """Load Sigma rules directly (bypasses config toggle for testing)."""
    rules = load_sigma_rules()
    assert len(rules) >= 27, f"Expected >=27 Sigma rules, got {len(rules)}"
    return rules


def _check(rules, command: str) -> SignatureVerdict:
    for pattern, technique, rule_id, desc in rules:
        if pattern.search(command):
            return SignatureVerdict(matched=True, technique=technique, rule_id=rule_id, description=desc)
    return SignatureVerdict(matched=False)


class TestSigmaCanaryBenign:
    """Benign agent commands that must not trigger any Sigma rule."""

    @pytest.mark.parametrize("cmd", [
        # Filesystem inspection
        "ls -la /var/log/",
        "cat /var/log/syslog | grep error",
        "tail -f /var/log/nginx/access.log",
        "head -100 /etc/nginx/nginx.conf",
        "find . -name '*.py' -type f",
        "du -sh /var/log/*",
        "df -h",
        "stat /etc/resolv.conf",
        # System diagnostics
        "free -m",
        "ps aux | sort -rk 4 | head -20",
        "uptime",
        "whoami",
        "hostname",
        "uname -a",
        "cat /etc/hosts",
        # Service management
        "systemctl status nginx",
        "systemctl restart nginx",
        "journalctl -u nginx --no-pager -n 50",
        # Cloud CLI
        "kubectl get pods -n default",
        "kubectl describe pod nginx-abc123",
        "kubectl logs nginx-abc123 --tail=100",
        "aws ec2 describe-instances --region us-east-1",
        "aws s3 ls",
        "gcloud compute instances list",
        # Docker
        "docker ps -a",
        "docker logs my-container --tail 50",
        # Network diagnostics
        "curl https://example.com",
        "wget https://example.com/file.txt",
        "ping -c 3 8.8.8.8",
        "dig example.com",
        "nslookup example.com",
        # Git
        "git status",
        "git log --oneline -10",
        "git diff HEAD~1",
        # Development
        "python3 -m pytest",
        "pip install boto3 requests",
        "npm install",
        # Shell operations
        "bash -c 'echo hello'",
        "/bin/sh -c 'echo test'",
        "/bin/bash script.sh",
        "echo hello world",
        "grep -r pattern .",
        # Terraform
        "terraform plan -out=plan.tfplan",
        "terraform init",
        # Safe rm
        "rm -rf /tmp/test-dir",
        "rm /tmp/myfile.txt",
        # SSH
        "ssh user@host",
        "scp file.txt user@host:/tmp/",
        # SRE diagnostics (suppressed rules / FP-prone patterns)
        "find / -perm -4000 -type f",
        "env -i /bin/bash --noprofile --norc",
        "nohup my-app /tmp/data.csv &",
        "cp /tmp/myapp/passwd-config.json .",
    ])
    def test_benign_commands(self, sigma_rules, cmd):
        v = _check(sigma_rules, cmd)
        assert not v.matched, (
            f"False positive on benign command: {cmd!r} "
            f"-> {v.rule_id}: {v.description}"
        )


class TestSigmaRulesDetectMalicious:
    """Commands that Sigma rules should catch (beyond hand-written rules)."""

    @pytest.mark.parametrize("cmd,expected_prefix", [
        ('/usr/bin/awk \'BEGIN {system("/bin/bash")}\'', "sigma-"),
        ("/usr/bin/capsh --", "sigma-"),
        ("/usr/bin/find . -exec /bin/sh ;", "sigma-"),
        ("/usr/bin/flock -u /tmp/foo /bin/bash", "sigma-"),
        ("/usr/bin/nice /bin/bash", "sigma-"),
        ("/usr/bin/nohup /tmp/payload", "sigma-"),
        ('/usr/bin/vim -c ":!/bin/bash"', "sigma-"),
        ("/usr/bin/rsync -e /bin/sh remote:/tmp/x /tmp/y", "sigma-"),
        ("/usr/bin/rm /home/user/.bash_history", "sigma-"),
        ("--cpu-priority=5 --donate-level=0", "sigma-"),
        ("/usr/bin/sudo execve_hijack", "sigma-"),
    ])
    def test_sigma_catches_malicious(self, sigma_rules, cmd, expected_prefix):
        v = _check(sigma_rules, cmd)
        assert v.matched, f"Sigma missed: {cmd!r}"
        assert v.rule_id.startswith(expected_prefix), (
            f"Expected Sigma rule for {cmd!r}, got {v.rule_id}"
        )
