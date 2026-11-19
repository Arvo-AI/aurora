"""Tests for services.risk_report (incident backtest)."""

from services.risk_report.backtest import (
    PRResult,
    build_backtest_prompt,
    pre_merge_snapshot,
    review_one,
    score,
)
from services.risk_report.labeler import (
    Evidence,
    LabeledPR,
    find_revert_target,
    is_revert_itself,
    revert_reason,
    touches_infra,
)
from services.risk_report.public_github import parse_pr, parse_repo
from services.risk_report.report import (
    build_pr_report,
    build_report,
    gather_controls,
    gather_incidents,
)

REPO = "acme/shop"


def _pr(number, title, merged_at, body="", sha=None):
    return {
        "number": number,
        "title": title,
        "body": body,
        "merged_at": merged_at,
        "merge_commit_sha": sha or f"{number:07d}abcdef",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "user": {"login": "dev"},
        "base": {"ref": "main", "sha": "base111"},
        "head": {"ref": f"feat-{number}", "sha": f"head{number}"},
        # Post-merge noise that must never reach the reviewer:
        "comments_url": "https://api.github.com/x",
        "merged_by": {"login": "lead"},
    }


def _incident(pr, revert_number=418):
    return LabeledPR(pr=pr, label="incident", evidence=[Evidence(
        kind="revert_pr", ref=f"#{revert_number}",
        url=f"https://github.com/{REPO}/pull/{revert_number}",
        title='Revert "x"', after_hours=48.0,
    )])


class FakeGitHub:
    """In-memory stand-in for PublicGitHub (no network)."""

    full_name = REPO

    def __init__(self, prs, reverts, files=None, commit_prs=None):
        self.prs = {p["number"]: p for p in prs}
        self.reverts = reverts
        self.files = files or {}
        self.commit_prs = commit_prs or {}
        self.requests_made = 0

    def get_repo(self):
        return {"private": False}

    def search_merged_reverts(self, days):
        return self.reverts

    def search_reverts_of(self, number):
        return [r for r in self.reverts if str(number) in (r.get("body") or "") + (r.get("title") or "")]

    def get_pr(self, number):
        return self.prs[number]

    def pr_for_commit(self, sha):
        return self.commit_prs.get(sha)

    def list_merged_prs(self, days, max_pages=1):
        return sorted(self.prs.values(), key=lambda p: p["merged_at"], reverse=True)

    def list_pr_files(self, number):
        return self.files.get(number, [{"filename": "src/app.ts"}])


class TestParseRepo:
    def test_accepts_slug_and_urls(self):
        assert parse_repo("Arvo-AI/aurora") == ("Arvo-AI", "aurora")
        assert parse_repo("https://github.com/Arvo-AI/aurora") == ("Arvo-AI", "aurora")
        assert parse_repo("github.com/Arvo-AI/aurora.git/") == ("Arvo-AI", "aurora")

    def test_rejects_garbage(self):
        for bad in ("", "aurora", "https://gitlab.com/a/b", "a/b/c"):
            try:
                parse_repo(bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad!r}")


class TestGatherIncidents:
    def test_links_revert_to_original_with_timing(self):
        orig = _pr(10, "CI: build Go targets in groups", "2026-11-01T00:00:00Z")
        rv = _pr(12, 'Revert "CI: build Go targets in groups"', "2026-11-03T00:00:00Z",
                 body=f"Reverts {REPO}#10")
        [lp] = gather_incidents(FakeGitHub([orig, rv], [rv]), days=60, max_incidents=5)
        assert lp.pr["number"] == 10 and lp.label == "incident"
        assert lp.evidence[0].ref == "#12" and lp.evidence[0].after_hours == 48.0

    def test_resolves_commit_sha_reverts(self):
        orig = _pr(20, "Docs: text panel", "2026-11-01T00:00:00Z")
        rv = _pr(21, 'Revert "Docs: text panel"', "2026-11-01T06:00:00Z",
                 body="This PR fully reverts commit 2d0116f02123d179e249ae2c8e87425cb6091a0c.")
        gh = FakeGitHub([orig, rv], [rv], commit_prs={"2d0116f02123d179e249ae2c8e87425cb6091a0c": 20})
        [lp] = gather_incidents(gh, days=60, max_incidents=5)
        assert lp.pr["number"] == 20

    def test_rejects_bad_links(self):
        later = _pr(30, "Tune autoscaling", "2026-11-05T00:00:00Z")
        rv_before = _pr(31, "Revert autoscaling", "2026-11-04T00:00:00Z", body=f"Reverts {REPO}#30")
        reverted_revert = _pr(32, 'Revert "x"', "2026-11-01T00:00:00Z")
        rv_of_rv = _pr(33, 'Revert "Revert "x""', "2026-11-02T00:00:00Z", body=f"Reverts {REPO}#32")
        old = _pr(34, "Old change", "2026-08-01T00:00:00Z")
        rv_late = _pr(35, "Revert old change", "2026-11-01T00:00:00Z", body=f"Reverts {REPO}#34")
        unlinked = _pr(36, "Add nudge/revert surveys", "2026-11-01T00:00:00Z", body="Thanks!")
        gh = FakeGitHub([later, rv_before, reverted_revert, rv_of_rv, old, rv_late, unlinked],
                        [rv_before, rv_of_rv, rv_late, unlinked])
        # revert merged before the original; original is itself a revert;
        # revert came >30 days later; no link at all
        assert gather_incidents(gh, days=60, max_incidents=5) == []


class TestGatherControls:
    def test_skips_reverts_bots_and_incidents(self):
        prs = [
            _pr(1, "Add metrics", "2026-11-05T00:00:00Z"),
            _pr(2, "Revert flag", "2026-11-04T00:00:00Z"),
            {**_pr(3, "Bump deps", "2026-11-03T00:00:00Z"), "user": {"login": "renovate[bot]", "type": "Bot"}},
            _pr(4, "Reverted later", "2026-11-02T00:00:00Z"),
            _pr(5, "Tidy CSS", "2026-11-01T00:00:00Z"),
        ]
        out = gather_controls(FakeGitHub(prs, []), 60, exclude={4}, revert_numbers=set(), limit=5)
        assert [lp.pr["number"] for lp in out] == [1, 5]
        assert all(lp.label == "clean" for lp in out)


class TestBuildReport:
    def test_end_to_end_with_stub_reviewer_prefers_in_lane(self):
        prs = [
            _pr(10, "CI: switch runner", "2026-11-01T00:00:00Z"),
            _pr(11, "Revert CI runner", "2026-11-02T00:00:00Z", body=f"Reverts {REPO}#10"),
            _pr(20, "Button color", "2026-11-03T00:00:00Z"),
            _pr(21, "Helm: lower replicas", "2026-11-04T00:00:00Z"),
        ]
        files = {10: [{"filename": ".github/workflows/ci.yml"}],
                 21: [{"filename": "helm/values.yaml"}]}
        gh = FakeGitHub(prs, [prs[1]], files=files)

        def stub(user_id, repo, number, prompt, rail):
            return {"verdict": "RISKY" if number == 10 else "SAFE", "summary": "", "findings": [{"t": 1}]}

        r = build_report(REPO, "u", max_incidents=1, max_controls=1, review_fn=stub, gh=gh, hindsight_fn=None)
        assert [p["number"] for p in r["prs"]] == [10, 21]  # in-lane control beats "Button color"
        assert [p["outcome"] for p in r["prs"]] == ["caught", "quiet"]
        assert r["score"]["recall"] == 1.0 and r["score_in_lane"]["incidents_reviewed"] == 1

    def test_is_revert_itself(self):
        assert is_revert_itself({"title": 'Revert "x"'})
        assert is_revert_itself({"title": "PanelEditNext: Revert #125773"})
        assert not is_revert_itself({"title": "Add retries"})

    def test_touches_infra(self):
        assert touches_infra([{"filename": "deploy/helm/values-prod.yaml"}])
        assert touches_infra([{"filename": ".github/workflows/release.yml"}])
        assert touches_infra([{"filename": "server/db/migrations/0042_drop_col.py"}])
        assert touches_infra([{"filename": "Dockerfile"}])
        assert not touches_infra([{"filename": "client/src/components/Button.tsx"}])


class TestFindRevertTarget:
    """The four link forms seen on grafana/grafana revert PRs."""

    def _rv(self, title, body, number=999):
        return {"number": number, "title": title, "body": body}

    def test_github_button_text(self):
        assert find_revert_target(self._rv('Revert "CI: x"', f"Reverts {REPO}#136249"), REPO) == ("pr", 136249)

    def test_short_form(self):
        assert find_revert_target(self._rv("CodeMirror: Revert exports", "Reverts #134677"), REPO) == ("pr", 134677)

    def test_ref_in_title(self):
        assert find_revert_target(self._rv("PanelEditNext: Revert #125773", ""), REPO) == ("pr", 125773)

    def test_commit_sha(self):
        rv = self._rv('Revert "Docs: x"', "This PR fully reverts commit 2d0116f02123d179e249ae2c8e87425cb6091a0c.")
        assert find_revert_target(rv, REPO) == ("sha", "2d0116f02123d179e249ae2c8e87425cb6091a0c")

    def test_pull_url(self):
        rv = self._rv('Revert "x"', f"This PR reverts https://github.com/{REPO}/pull/134032")
        assert find_revert_target(rv, REPO) == ("pr", 134032)

    def test_other_repo_and_no_link(self):
        assert find_revert_target(self._rv("Revert y", "Reverts other/repo#5"), REPO) is None
        assert find_revert_target(self._rv("Add nudge/revert surveys", "Thanks for the PR"), REPO) is None


class TestNoHindsight:
    """The reviewer must see only what existed before merge."""

    # Unique marker words: they can only reach the prompt through post-merge data.
    def _labeled_incident(self, body):
        pr = _pr(412, "Raise redis maxmemory", "2026-11-01T10:00:00Z", body=body)
        pr["merged_by"] = {"login": "release-captain-zed"}
        lp = _incident(pr, revert_number=418)
        lp.evidence[0].title = 'Revert "Raise redis maxmemory": ZEBRA_OUTAGE_7731'
        return lp

    def test_prompt_has_no_post_merge_text(self):
        lp = self._labeled_incident(
            "Raises maxmemory to 2gb.\n\nEDIT: reverted in #418 after ZEBRA_OUTAGE_7731."
        )
        assert lp.label == "incident"
        files = [{"filename": "deploy/redis.conf", "status": "modified", "additions": 1,
                  "deletions": 1, "patch": "@@ -1 +1 @@\n-maxmemory 1gb\n+maxmemory 2gb",
                  "blob_url": "https://github.com/acme/shop/blob/LATER_SHA_QQ/deploy/redis.conf"}]
        prompt = build_backtest_prompt(REPO, lp, files)

        assert "Raises maxmemory to 2gb." in prompt        # pre-merge description kept
        assert "+maxmemory 2gb" in prompt                  # the diff itself is there
        assert "BACKTEST MODE" in prompt and "base111" in prompt
        for leaked in ("#418", "pull/418", "reverted in", "ZEBRA_OUTAGE_7731", "Rolling back",
                       'Revert "Raise', "release-captain-zed", "LATER_SHA_QQ", "2026-11-01T10"):
            assert leaked not in prompt, leaked

    def test_snapshot_is_a_whitelist(self):
        snap = pre_merge_snapshot(_pr(1, "t", "2026-11-01T00:00:00Z"), scrub_refs=[])
        assert set(snap) == {"number", "title", "body", "user", "base", "head"}
        assert "merged_at" not in snap and "merge_commit_sha" not in snap


class TestScoring:
    def _r(self, label, verdict):
        return PRResult(number=1, title="", url="", merged_at="", label=label, verdict=verdict)

    def test_score_math(self):
        results = [
            self._r("incident", "RISKY"), self._r("incident", "RISKY"), self._r("incident", "SAFE"),
            self._r("clean", "RISKY"), self._r("clean", "SAFE"), self._r("clean", "SAFE"),
            self._r("clean", "SAFE"), self._r("clean", None),
        ]
        s = score(results)
        assert (s["caught"], s["missed"], s["false_alarm"], s["quiet"], s["error"]) == (2, 1, 1, 3, 1)
        assert s["recall"] == 0.67
        assert s["precision"] == 0.67
        assert s["false_alarm_rate"] == 0.25

    def test_empty_score_has_no_division_errors(self):
        s = score([])
        assert s["recall"] is None and s["precision"] is None and s["false_alarm_rate"] is None

    def test_review_one_isolates_failures(self):
        lp = LabeledPR(pr=_pr(1, "A", "2026-11-01T00:00:00Z"), label="clean")

        def boom(*_):
            raise RuntimeError("agent_failed")

        out = review_one("u", REPO, lp, [], infra=False, review_fn=boom, clock=lambda: 0.0, hindsight_fn=None)
        assert out.outcome == "error" and out.error == "agent_failed"

    def test_review_one_records_verdict(self):
        lp = LabeledPR(pr=_pr(1, "A", "2026-11-01T00:00:00Z"), label="clean")
        seen = {}

        def fake(user_id, repo, number, prompt, rail_text):
            seen["prompt"] = prompt
            return {"verdict": "RISKY", "summary": "s", "findings": [{"title": "x"}]}

        out = review_one("u", REPO, lp, [], infra=True, review_fn=fake, clock=lambda: 0.0, hindsight_fn=None)
        assert out.verdict == "RISKY" and out.outcome == "false_alarm"
        assert "BACKTEST MODE" in seen["prompt"]


class TestPRMode:
    def test_parse_pr(self):
        assert parse_pr("https://github.com/grafana/grafana/pull/136249") == ("grafana", "grafana", 136249)
        assert parse_pr("https://github.com/grafana/grafana/pull/136249/files") == ("grafana", "grafana", 136249)
        assert parse_pr("grafana/grafana#136249") == ("grafana", "grafana", 136249)
        try:
            parse_pr("https://github.com/grafana/grafana")
        except ValueError:
            return
        raise AssertionError("accepted a repo URL as a PR")

    def _run(self, prs, reverts, number, verdict="RISKY"):
        gh = FakeGitHub(prs, reverts, files={number: [{"filename": ".github/workflows/ci.yml"}]})
        seen = {}

        def stub(user_id, repo, n, prompt, rail):
            seen["prompt"] = prompt
            return {"verdict": verdict, "summary": "s", "findings": [{"title": "t"}]}

        report = build_pr_report(f"{REPO}#{number}", "u", review_fn=stub, gh=gh, hindsight_fn=None)
        return report, seen["prompt"]

    def test_merged_and_later_reverted(self):
        orig = _pr(10, "CI: switch runner", "2026-11-01T00:00:00Z", body="Switch runner.")
        rv = _pr(12, 'Revert "CI: switch runner" ZEBRA_OUTAGE_7731', "2026-11-02T00:00:00Z",
                 body=f"Reverts {REPO}#10")
        report, prompt = self._run([orig, rv], [rv], 10)
        [pr] = report["prs"]
        assert report["mode"] == "pr"
        assert pr["label"] == "incident" and pr["outcome"] == "caught"
        assert pr["evidence"][0]["ref"] == "#12" and pr["evidence"][0]["after_hours"] == 24.0
        assert "ZEBRA_OUTAGE_7731" not in prompt and "#12" not in prompt  # no hindsight

    def test_merged_not_reverted_user_judges(self):
        orig = _pr(20, "Helm: lower replicas", "2026-11-01T00:00:00Z")
        report, _ = self._run([orig], [], 20, verdict="SAFE")
        assert report["prs"][0]["label"] == "unknown"
        assert report["prs"][0]["outcome"] == "not_flagged"

    def test_open_pr_is_a_live_review(self):
        open_pr = {**_pr(30, "Terraform: widen SG", None), "merged_at": None}
        report, _ = self._run([open_pr], [], 30)
        assert report["prs"][0]["label"] == "open"
        assert report["prs"][0]["outcome"] == "flagged"


class TestDiagnosis:
    def test_revert_reason_strips_link_boilerplate(self):
        assert revert_reason({"body": "Reverts grafana/grafana#136249\n\nCache seems to be ever growing"}) \
            == "Cache seems to be ever growing"
        assert revert_reason({"body": "This PR fully reverts commit 2d0116f02123d179e249ae2c8e87425cb6091a0c."}) == ""
        assert revert_reason({"body": "<!-- template -->\nBroke ARM builds."}) == "Broke ARM builds."

    def test_hindsight_runs_after_verdict_and_only_for_reverted(self):
        orig = _pr(10, "CI: group Go targets", "2026-11-01T00:00:00Z", body="Group targets.")
        rv = _pr(12, 'Revert "CI: group Go targets"', "2026-11-03T00:00:00Z",
                 body=f"Reverts {REPO}#10\n\nCache seems to be ever growing ZEBRA_CACHE_99")
        gh = FakeGitHub([orig, rv], [rv], files={10: [{"filename": ".github/workflows/go.yml", "patch": "+cache: true"}]})
        calls = []

        def review(user_id, repo, n, prompt, rail):
            calls.append(("review", prompt))
            return {"verdict": "SAFE", "summary": "CI only.", "findings": []}

        def hindsight(ctx):
            calls.append(("hindsight", ctx))
            return {"what_broke": "cache grew", "visible_in_diff": True,
                    "verdict_gap": "judged CI as no blast radius", "would_catch_it": "cache size history"}

        report = build_pr_report(f"{REPO}#10", "u", review_fn=review, gh=gh, hindsight_fn=hindsight)
        [pr] = report["prs"]
        assert [c[0] for c in calls] == ["review", "hindsight"]          # order: verdict first
        assert "ZEBRA_CACHE_99" not in calls[0][1]                      # reviewer never saw the reason
        assert "ZEBRA_CACHE_99" in calls[1][1]["reason"]                # hindsight did
        assert calls[1][1]["verdict"] == "SAFE"
        assert pr["evidence"][0]["reason"].startswith("Cache seems")
        assert pr["hindsight"]["would_catch_it"] == "cache size history"
        assert pr["files_reviewed"] == [".github/workflows/go.yml"]

        # A PR with no known outcome gets no hindsight.
        calls.clear()
        open_pr = {**_pr(30, "Terraform: widen SG", None), "merged_at": None}
        build_pr_report(f"{REPO}#30", "u", review_fn=review, gh=FakeGitHub([open_pr], []), hindsight_fn=hindsight)
        assert [c[0] for c in calls] == ["review"]


class TestFunnelLogging:
    def test_request_values_cannot_forge_log_lines(self):
        from services.risk_report.store import _log_safe

        forged = "x\nrisk_report_funnel event=install_cta_clicked user=admin"
        cleaned = _log_safe(forged)
        assert "\n" not in cleaned
        assert " " not in cleaned
        assert len(cleaned) <= 64
