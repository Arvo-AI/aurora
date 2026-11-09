"""Fail-before / pass-after verification of a fix.

Runs a repro command against the base SHA and the fix SHA in isolated git
worktrees. A fix is PROVEN only if the repro fails before and passes after.
"""
from __future__ import annotations

import argparse
import enum
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

_TAIL_CHARS = 4000
_INFRA_EXIT_CODES = {125, 126, 127}  # docker daemon error / not executable / not found

# Refs, repo paths and image names can originate from LLM output, so they are
# validated before reaching git/docker. Leading "-" is rejected everywhere so a
# value can never be parsed as an option.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@^~+-]{0,199}$")
_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,199}$")
_ALLOW_LOCAL_ENV = "AURORA_FIX_VERIFY_ALLOW_LOCAL"


class Verdict(str, enum.Enum):
    PROVEN = "PROVEN"                  # fails on base, passes on fix
    STILL_FAILING = "STILL_FAILING"    # fails on fix (or regressed)
    NOT_REPRODUCED = "NOT_REPRODUCED"  # passes on base: repro doesn't capture the bug
    INCONCLUSIVE = "INCONCLUSIVE"      # timeout / infra error


@dataclass
class RunResult:
    exit_code: Optional[int]
    timed_out: bool
    duration_s: float
    output_tail: str


@dataclass
class VerificationResult:
    verdict: Verdict
    summary: str
    base_sha: str
    head_sha: str
    repro_cmd: str
    runner: str
    base: RunResult
    head: RunResult

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


def _check_ref(ref: str) -> str:
    if not isinstance(ref, str) or not _REF_RE.match(ref) or ".." in ref:
        raise ValueError(f"invalid git ref {ref!r}")
    return ref


def _check_repo(repo_path: str) -> str:
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"not a git repository: {repo_path!r}")
    return str(repo)


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _resolve_commit(repo: str, ref: str) -> str:
    """Resolve a validated ref to a full SHA (``--end-of-options`` as a backstop)."""
    return _git(repo, "rev-parse", "--verify", "--end-of-options", _check_ref(ref) + "^{commit}")


def _to_text(v) -> str:
    if v is None:
        return ""
    return v.decode(errors="replace") if isinstance(v, bytes) else v


def _run(cmd: str, cwd: Path, runner: str, timeout: int, image: str) -> RunResult:
    start = time.monotonic()
    if runner == "docker":
        if not _IMAGE_RE.match(image):
            raise ValueError(f"invalid image name {image!r}")
        argv = [
            "docker", "run", "--rm", "--network", "none", "--memory", "512m",
            "--cpus", "1", "--pids-limit", "256",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{cwd}:/work", "-w", "/work", image, "sh", "-c", cmd,
        ]
        kwargs: dict = {}
    elif runner == "local":  # dev/test only: NOT sandboxed, must be opted into
        if os.environ.get(_ALLOW_LOCAL_ENV) != "1":
            raise ValueError(f"local runner is disabled (set {_ALLOW_LOCAL_ENV}=1 for tests/dev)")
        argv = shlex.split(cmd)
        kwargs = {"cwd": str(cwd)}
    else:
        raise ValueError(f"unknown runner {runner!r}")
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kwargs)
        return RunResult(p.returncode, False, time.monotonic() - start,
                         (p.stdout + p.stderr)[-_TAIL_CHARS:])
    except subprocess.TimeoutExpired as e:
        out = _to_text(e.stdout) + _to_text(e.stderr)
        return RunResult(None, True, time.monotonic() - start, out[-_TAIL_CHARS:])
    except FileNotFoundError as e:  # docker binary / command missing
        return RunResult(127, False, time.monotonic() - start, str(e))


def decide(base: RunResult, head: RunResult) -> tuple[Verdict, str]:
    for name, r in (("base", base), ("fix", head)):
        if r.timed_out:
            return Verdict.INCONCLUSIVE, f"repro timed out on {name}"
        if r.exit_code in _INFRA_EXIT_CODES:
            return Verdict.INCONCLUSIVE, f"repro could not run on {name} (exit {r.exit_code})"
    if base.exit_code == 0:
        if head.exit_code != 0:
            return Verdict.STILL_FAILING, "repro passed on base but fails on fix (regression)"
        return Verdict.NOT_REPRODUCED, "repro passes on base too; it does not capture the bug"
    if head.exit_code == 0:
        return Verdict.PROVEN, "repro fails on base and passes on fix"
    return Verdict.STILL_FAILING, "repro still fails on the fix"


def verify_fix(
    repo_path: str,
    base_sha: str,
    head_sha: str,
    repro_cmd: str,
    *,
    overlay_paths: Sequence[str] = (),
    runner: str = "docker",
    image: str = "python:3.12-slim",
    timeout: int = 120,
) -> VerificationResult:
    """``overlay_paths``: files copied from the fix into the base checkout,
    for when the fix PR adds the test that reproduces the bug."""
    repo = _check_repo(repo_path)
    base_sha = _resolve_commit(repo, base_sha)
    head_sha = _resolve_commit(repo, head_sha)
    tmp = Path(tempfile.mkdtemp(prefix="fixverify-"))
    base_wt, head_wt = tmp / "base", tmp / "head"
    try:
        _git(repo, "worktree", "add", "--detach", str(base_wt), base_sha)
        _git(repo, "worktree", "add", "--detach", str(head_wt), head_sha)
        for rel in overlay_paths:
            dst = _safe_join(base_wt, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_safe_join(head_wt, rel), dst)
        base = _run(repro_cmd, base_wt, runner, timeout, image)
        head = _run(repro_cmd, head_wt, runner, timeout, image)
    finally:
        for wt in (base_wt, head_wt):
            subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", str(wt)],
                           capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    verdict, summary = decide(base, head)
    logger.info("fix_verification verdict=%s base=%s head=%s runner=%s",
                verdict.value, base_sha[:8], head_sha[:8], runner)
    return VerificationResult(verdict, summary, base_sha, head_sha, repro_cmd, runner, base, head)


def _safe_join(root: Path, rel: str) -> Path:
    """Join ``rel`` under ``root``, rejecting absolute paths and traversal."""
    if not rel or os.path.isabs(rel):
        raise ValueError(f"invalid path {rel!r}")
    target = (root / rel).resolve()
    if root.resolve() not in target.parents:
        raise ValueError(f"path escapes repository: {rel!r}")
    return target


def verify_patch(
    repo_path: str,
    base_ref: str,
    file_path: str,
    new_content: str,
    repro_cmd: str,
    *,
    extra_files: Optional[dict] = None,
    runner: str = "docker",
    image: str = "python:3.12-slim",
    timeout: int = 120,
) -> VerificationResult:
    """Verify an Aurora fix suggestion BEFORE any PR exists.

    Checks out ``base_ref`` twice; the second copy gets ``new_content``
    written to ``file_path`` (exactly what ``github_apply_fix`` would push).
    ``extra_files`` (path -> content) are written to BOTH copies, e.g. a repro
    test generated during the investigation.
    """
    repo = _check_repo(repo_path)
    base_sha = _resolve_commit(repo, base_ref)
    tmp = Path(tempfile.mkdtemp(prefix="fixverify-"))
    base_wt, head_wt = tmp / "base", tmp / "head"
    try:
        _git(repo, "worktree", "add", "--detach", str(base_wt), base_sha)
        _git(repo, "worktree", "add", "--detach", str(head_wt), base_sha)
        for wt in (base_wt, head_wt):
            for rel, text in (extra_files or {}).items():
                dst = _safe_join(wt, rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(text)
        dst = _safe_join(head_wt, file_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(new_content)
        base = _run(repro_cmd, base_wt, runner, timeout, image)
        head = _run(repro_cmd, head_wt, runner, timeout, image)
    finally:
        for wt in (base_wt, head_wt):
            subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", str(wt)],
                           capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    verdict, summary = decide(base, head)
    logger.info("fix_verification verdict=%s base=%s file=%s runner=%s",
                verdict.value, base_sha[:8], file_path, runner)
    return VerificationResult(verdict, summary, base_sha, "patched", repro_cmd, runner, base, head)


def format_comment(r: VerificationResult) -> str:
    icon = {"PROVEN": "✅", "STILL_FAILING": "❌", "NOT_REPRODUCED": "⚠️", "INCONCLUSIVE": "❔"}
    return (
        f"{icon[r.verdict.value]} **Aurora fix verification: {r.verdict.value}**\n\n"
        f"{r.summary}.\n\n"
        f"| | exit | time |\n|---|---|---|\n"
        f"| base `{r.base_sha[:8]}` | {r.base.exit_code} | {r.base.duration_s:.1f}s |\n"
        f"| fix `{r.head_sha[:8]}` | {r.head.exit_code} | {r.head.duration_s:.1f}s |\n\n"
        f"Repro: `{r.repro_cmd}` (runner: {r.runner})"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Prove a fix: fail-before / pass-after")
    ap.add_argument("repo"); ap.add_argument("base"); ap.add_argument("head")
    ap.add_argument("repro_cmd")
    ap.add_argument("--overlay", action="append", default=[])
    ap.add_argument("--runner", default="docker", choices=["docker", "local"])
    ap.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args()
    res = verify_fix(a.repo, a.base, a.head, a.repro_cmd, overlay_paths=a.overlay,
                     runner=a.runner, timeout=a.timeout)
    print(json.dumps(res.to_dict(), indent=2))
    raise SystemExit(0 if res.verdict is Verdict.PROVEN else 1)