"""Revert replay on any public GitHub repo, with nothing installed.

    cd server
    python -m services.change_gating.replay_cli owner/repo [--reverted 5] [--controls 5]

The zero-trust door to Incident Prevention: no GitHub App, no account, no
connected systems. It reads the repo's recent reverted PRs and a control set
of PRs that shipped fine, asks the configured model (``MAIN_MODEL``, same
provider env vars as the server) for the normal pre-merge review of each
original diff, and prints the scorecard. ``GITHUB_TOKEN`` is optional and
only raises GitHub's rate limit; replay never writes anything.

This is the diff-only mode: one model call per PR, no tools. Inside Aurora
(``tasks.change_gating_replay``) the same replay runs through the full review
agent with the customer's connected context.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional


def _llm_reviewer(model: Optional[str]):
    from langchain_core.messages import HumanMessage

    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.providers import create_chat_model
    from services.change_gating.replay import build_replay_prompt
    from services.change_gating.verdict import normalize_verdict, parse_verdict

    llm = create_chat_model(model or ModelConfig.MAIN_MODEL, temperature=0.0, streaming=False)

    def review(pr: dict[str, Any], files: list, diff: Optional[str]) -> dict[str, Any]:
        repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name") or ""
        prompt = build_replay_prompt(repo, pr, files, diff, tools_available=False)
        content = llm.invoke([HumanMessage(content=prompt)]).content
        if isinstance(content, list):  # Anthropic content blocks
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        verdict = parse_verdict(content)
        if not verdict:
            raise ValueError("verdict_parse_failed")
        return normalize_verdict(verdict)

    return review


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo", help="owner/name of a public GitHub repository")
    parser.add_argument("--reverted", type=int, default=5, help="reverted PRs to replay")
    parser.add_argument("--controls", type=int, default=5, help="never-reverted PRs to replay")
    parser.add_argument("--model", default=None, help="override MAIN_MODEL")
    parser.add_argument("--json", dest="json_path", help="also write the full report here")
    parser.add_argument("--list-only", action="store_true",
                        help="only list the cases that would be replayed (no model calls)")
    args = parser.parse_args(argv)

    from services.change_gating.github_adapter import PublicRepoReader
    from services.change_gating.replay import run_replay

    reader = PublicRepoReader(args.repo, token=os.getenv("GITHUB_TOKEN"))
    try:
        if args.list_only:
            from services.change_gating.replay import collect_cases

            _, cases, _ = collect_cases(
                reader, args.repo,
                reverted_limit=args.reverted, control_limit=args.controls,
            )
            for case in cases:
                via = f" (reverted by #{case.revert_pr_number}, {case.match_rule})" if case.revert_pr_number else ""
                print(f"{case.label:9} #{case.pr_number} {case.title}{via}")
            return 0

        def progress(partial: dict[str, Any]) -> None:
            last = partial["results"][-1]
            print(
                f"[{len(partial['results'])}/{partial['total_cases']}] "
                f"#{last['case']['pr_number']} ({last['case']['label']}): "
                f"{last.get('verdict') or last.get('error')}",
                file=sys.stderr,
            )

        from services.change_gating.replay import render_markdown

        report = run_replay(
            reader, args.repo,
            review=_llm_reviewer(args.model),
            reverted_limit=args.reverted, control_limit=args.controls,
            on_progress=progress, mode="diff_only",
        )
    finally:
        reader.close()

    if args.json_path:
        with open(args.json_path, "w") as fh:
            json.dump(report, fh, indent=2)
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
