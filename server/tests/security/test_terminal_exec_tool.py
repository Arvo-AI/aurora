"""Tests for terminal_exec_tool -- shell-routing decision.

``_has_shell_metacharacters`` decides whether a command gets handed to a
real shell (where metacharacters are interpreted) or tokenised and
passed straight to exec.  False negatives let an attacker smuggle shell
syntax past the routing gate; false positives quietly route plain
commands through a shell with no need.
"""

import ast
import os
import pathlib
import sys

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))


# ---------------------------------------------------------------------------
# Function loader
# ---------------------------------------------------------------------------
# ``terminal_exec_tool`` pulls in heavy transitive deps (langchain_core,
# boto3, google.cloud.*).  None of them are relevant to the pure
# functions under test.  We extract just the function definition via AST
# parsing and exec it into an empty namespace, which sidesteps the
# entire import graph.

_SOURCE_FILE = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "chat" / "backend" / "agent" / "tools" / "terminal_exec_tool.py"
)


def _load_function(name: str):
    """Extract a top-level function from the source file by name."""
    tree = ast.parse(_SOURCE_FILE.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            namespace: dict = {}
            exec(compile(module, str(_SOURCE_FILE), "exec"), namespace)
            return namespace[name]
    raise LookupError(f"function {name!r} not found in {_SOURCE_FILE}")


_has_shell_metacharacters = _load_function("_has_shell_metacharacters")


# ---------------------------------------------------------------------------
# Plain commands -> False (no false positives)
# ---------------------------------------------------------------------------


class TestPlainCommandsReturnFalse:
    """Routine SRE commands must NOT be flagged as needing a shell."""

    @pytest.mark.parametrize("cmd", [
        "ls",
        "ls -la",
        "kubectl get pods",
        "kubectl describe pod nginx-abc123",
        "aws s3 ls",
        "aws ec2 describe-instances --region us-east-1",
        "gcloud compute instances list",
        "echo hello",
        "cat /etc/hosts",
        "git status",
        "terraform plan",
        "docker ps",
    ])
    def test_plain_command_returns_false(self, cmd):
        """Plain command has no shell metacharacters."""
        assert _has_shell_metacharacters(cmd) is False


# ---------------------------------------------------------------------------
# Each metacharacter -> True (one positive per character, per design doc)
# ---------------------------------------------------------------------------


class TestMetacharactersTriggerTrue:
    """One positive case per metacharacter in the patterns list."""

    def test_pipe(self):
        """`|` (pipe) routes commands through a shell."""
        assert _has_shell_metacharacters("cat foo | grep bar") is True

    def test_logical_or(self):
        """`||` (logical OR) routes through a shell."""
        assert _has_shell_metacharacters("cmd1 || cmd2") is True

    def test_logical_and(self):
        """`&&` (logical AND) routes through a shell."""
        assert _has_shell_metacharacters("cmd1 && cmd2") is True

    def test_semicolon(self):
        """`;` (command separator) routes through a shell."""
        assert _has_shell_metacharacters("cmd1; cmd2") is True

    def test_dollar_paren_substitution(self):
        """`$(...)` (command substitution) routes through a shell."""
        assert _has_shell_metacharacters("echo $(whoami)") is True

    def test_backtick_substitution(self):
        """Backtick substitution routes through a shell."""
        assert _has_shell_metacharacters("echo `whoami`") is True

    def test_stderr_redirect(self):
        """` 2>` (stderr redirect) routes through a shell."""
        assert _has_shell_metacharacters("cmd 2>err.log") is True

    def test_stderr_to_stdout(self):
        """`2>&1` (merge stderr into stdout) routes through a shell."""
        assert _has_shell_metacharacters("cmd 2>&1") is True

    def test_stdout_redirect(self):
        """` > ` (stdout redirect) routes through a shell."""
        assert _has_shell_metacharacters("cmd > out.log") is True

    def test_stdout_append(self):
        """` >> ` (stdout append) routes through a shell."""
        assert _has_shell_metacharacters("cmd >> out.log") is True

    def test_stdin_redirect(self):
        """` < ` (stdin redirect) routes through a shell."""
        assert _has_shell_metacharacters("cmd < in.txt") is True

    def test_background_amp(self):
        """` & ` (background process) routes through a shell."""
        assert _has_shell_metacharacters("cmd1 & cmd2") is True


# ---------------------------------------------------------------------------
# Redirect-at-start (separate branch in the function)
# ---------------------------------------------------------------------------


class TestRedirectsAtCommandStart:
    """A command starting with a redirect must route through a shell."""

    @pytest.mark.parametrize("cmd", [
        ">foo",
        ">>foo",
        "<foo",
        "2>foo",
    ])
    def test_redirect_prefix_returns_true(self, cmd):
        """Leading redirect token is detected by lstrip().startswith."""
        assert _has_shell_metacharacters(cmd) is True

    def test_leading_whitespace_does_not_hide_redirect(self):
        """Whitespace before a leading redirect is stripped before the check."""
        assert _has_shell_metacharacters("   >foo") is True


# ---------------------------------------------------------------------------
# Pinned non-detections (per design doc: "Pin the answer for...")
# ---------------------------------------------------------------------------


class TestPinnedBehavior:
    """Lock down current answers for cases the design doc calls out.

    These assertions document *current* behaviour, not necessarily the
    desired security posture.  If the gate is later tightened (e.g. to
    catch newlines), these tests should be updated as part of the same
    deliberate change -- the failure surface makes the regression visible.
    """

    def test_newline_is_not_a_metacharacter(self):
        r"""`cmd1\ncmd2` is NOT flagged -- newline is absent from the list."""
        assert _has_shell_metacharacters("cmd1\ncmd2") is False

    @pytest.mark.parametrize("cmd", [
        "ls *.txt",
        "cat ?.log",
        "ls [abc].txt",
    ])
    def test_globs_are_not_metacharacters(self, cmd):
        """Globs (`*`, `?`, `[abc]`) are NOT flagged -- delegated downstream."""
        assert _has_shell_metacharacters(cmd) is False

    def test_redirect_without_spaces_in_middle_not_flagged(self):
        """`>` mid-word with no surrounding spaces is NOT flagged."""
        assert _has_shell_metacharacters("cmd>foo") is False

    def test_amp_without_spaces_not_flagged(self):
        """`&` without surrounding spaces is NOT flagged."""
        assert _has_shell_metacharacters("cmd1&cmd2") is False

    def test_empty_string_returns_false(self):
        """Empty input has no metacharacters."""
        assert _has_shell_metacharacters("") is False
