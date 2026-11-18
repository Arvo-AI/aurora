"""Command-line entry points for incident-guided browser testing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .author import author_scenario
from .paths import ARTIFACT_DIRECTORY, AUTHORED_SCENARIO_FILE, scenario_file
from .runner import run_scenario
from .scenario import save_scenario


DEFAULT_MODEL = "gpt-5-mini"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aurora-browser-test")
    commands = parser.add_subparsers(dest="command", required=True)

    author = commands.add_parser("author", help="author and freeze a scenario")
    author.add_argument("--brief", required=True, type=Path)
    author.add_argument("--preview-url", required=True)
    author.add_argument("--incident-id", required=True)
    author.add_argument("--incident-reference", required=True)
    author.add_argument("--baseline-commit", required=True)
    author.add_argument("--model", default=DEFAULT_MODEL)

    run = commands.add_parser("run", help="run a frozen scenario")
    run.add_argument("--scenario", required=True, type=Path)
    run.add_argument("--preview-url", required=True)
    run.add_argument("--commit", required=True)
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument(
        "--target-url",
        action="append",
        default=[],
        metavar="TARGET=URL",
        help="URL for one frozen named target; repeat for multiple targets",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "author":
        brief = scenario_file(args.brief).read_text(encoding="utf-8").strip()
        scenario = author_scenario(
            brief=brief,
            preview_url=args.preview_url,
            incident_id=args.incident_id,
            incident_reference=args.incident_reference,
            baseline_commit=args.baseline_commit,
            model=args.model,
        )
        save_scenario(scenario)
        print(
            json.dumps(
                {
                    "scenario": str(AUTHORED_SCENARIO_FILE),
                    "scenarioHash": scenario["scenarioHash"],
                }
            )
        )
        return 0

    target_urls = {}
    for item in args.target_url:
        target, separator, url = item.partition("=")
        if not separator or not target or not url or target in target_urls:
            raise SystemExit("--target-url must be a unique TARGET=URL pair")
        target_urls[target] = url

    result = run_scenario(
        scenario_path=args.scenario,
        preview_url=args.preview_url,
        tested_commit=args.commit,
        model=args.model,
        target_urls=target_urls,
    )
    print(
        json.dumps(
            {
                "result": str(ARTIFACT_DIRECTORY / "browser-result.json"),
                "scenarioHash": result.get("scenarioHash"),
                "verdict": result["verdict"],
                "reason": result["reason"],
            }
        )
    )
    return {"passed": 0, "failed": 1, "inconclusive": 2}[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
