import subprocess
import sys

import pytest

from services.fix_verification import Verdict, verify_fix

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
CHECK = "from calc import add\nassert add(2, 3) == 5\n"
CMD = f"{sys.executable} check.py"


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "calc.py").write_text(BUGGY)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def _commit(repo, files, msg):
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _verify(repo, files, **kw):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, files, "fix")
    return verify_fix(str(repo), base, head, CMD, overlay_paths=["check.py"],
                      runner="local", **kw)


def test_proven_when_fix_adds_the_test(repo):
    assert _verify(repo, {"calc.py": FIXED, "check.py": CHECK}).verdict is Verdict.PROVEN


def test_still_failing_when_fix_is_wrong(repo):
    r = _verify(repo, {"calc.py": "def add(a, b):\n    return a * b\n", "check.py": CHECK})
    assert r.verdict is Verdict.STILL_FAILING


def test_not_reproduced_when_repro_is_vacuous(repo):
    r = _verify(repo, {"calc.py": FIXED, "check.py": "assert True\n"})
    assert r.verdict is Verdict.NOT_REPRODUCED


def test_inconclusive_on_timeout(repo):
    r = _verify(repo, {"calc.py": FIXED, "check.py": "import time; time.sleep(5)\n"}, timeout=1)
    assert r.verdict is Verdict.INCONCLUSIVE