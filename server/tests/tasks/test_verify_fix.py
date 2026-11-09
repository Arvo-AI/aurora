import subprocess
import sys

import pytest

from tasks.verify_fix import run_verification

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
REPRO = {"check.py": "from calc import add\nassert add(2, 3) == 5\n"}
CMD = f"{sys.executable} check.py"


@pytest.fixture
def repo(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "calc.py").write_text(BUGGY)
    git("add", "."); git("commit", "-qm", "base")
    return str(tmp_path)


def _run(repo, suggestion, **kw):
    return run_verification(suggestion, repo, CMD, extra_files=REPRO, runner="local", **kw)


def test_good_suggestion_is_proven(repo):
    r = _run(repo, {"id": 1, "file_path": "calc.py", "suggested_content": FIXED})
    assert r["verdict"] == "PROVEN"
    assert "PROVEN" in r["comment"]


def test_edited_content_is_used_when_present(repo):
    s = {"id": 1, "file_path": "calc.py", "suggested_content": FIXED,
         "user_edited_content": BUGGY}
    assert _run(repo, s)["verdict"] == "STILL_FAILING"
    assert _run(repo, s, use_edited_content=False)["verdict"] == "PROVEN"


def test_path_traversal_is_inconclusive_not_a_write(repo, tmp_path):
    s = {"id": 1, "file_path": "../evil.py", "suggested_content": "x"}
    r = _run(repo, s)
    assert r["verdict"] == "INCONCLUSIVE"
    assert not (tmp_path.parent / "evil.py").exists()


def test_missing_content_is_inconclusive(repo):
    assert _run(repo, {"id": 1, "file_path": "calc.py"})["verdict"] == "INCONCLUSIVE"