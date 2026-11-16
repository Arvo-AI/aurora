# Audit against the submission rules

Each row names the rule, what the deliverable does about it, and the file a reviewer can open to check. The rules are R1 to R11 of the assessment brief.

| Rule | What the deliverable does | Where to check |
|---|---|---|
| R1 | Every result carries a provenance label. A capture records `mode: LIVE`; a replay of a fixture records `SNAPSHOT`. The report prints the mode for the run and for each variant, and the JSON carries one `mode` per run. | `verifier/observations.py` (`Meta.mode`), `verifier/report.py` (`_mode`), header of `out/report.md` |
| R2 | The verdict is advisory. The command line says so, the README says so, and no target or workflow step presents the verifier as a required check on the `aurora` repository. | `verifier/cli.py` (`description`), `README.md`, `tests/test_workflow.py` |
| R3 | The only positive state is named `VERIFIED_WITHIN_CONTRACT`. No document claims a remediation succeeded without that phrase. | `verifier/decision.py`, `verifier/report.py`, `README.md` |
| R4 | Every check, fix and fixture has a source with a path and a line. The alternative fix's 384Mi comes from the runbook's 244MiB measurement plus a stated margin. | `contracts/scenario_12.yaml`, `contracts/PROVENANCE.md`, `contracts/DECISIONS.md` (D59) |
| R5 | No check accepts a fix by matching a constant. The six checks read the observation, and C1 shows each one flips when its evidence changes. | `verifier/checks.py`, `tests/test_controls.py` |
| R6 | `agent_view()` returns the alert and nothing else. Tests compare its keys with the oracle's and search its text for the second fault. | `verifier/contract.py`, `tests/test_contract_loader.py` (`test_the_agent_view_holds_only_the_alert`), `tests/test_contract.py` |
| R7 | Every claim about `aurora-lab` cites the pinned commit and a path with lines, in the README and in the provenance table. | `README.md` (`Sources`), `contracts/PROVENANCE.md` |
| R8 | No competitor name and no third-party benchmark number appears in the README or in the pull request text. The repository name of the scenario is cited because R7 requires the source. | `README.md`, `contracts/AUDIT.md` |
| R9 | Every metric prints its denominator: 6 checks, 3 negative controls, 1 positive control, 2 repeats. | metrics table of `out/report.md`, `tests/test_controls.py` (`test_the_control_metrics_carry_their_denominators`) |
| R10 | Every committed path starts with `remediation-verifier/`. | `git diff --stat main...HEAD` |
| R11 | A snapshot missing any required metadata is INCONCLUSIVE. The incomplete fixture and its test prove it, and no positive verdict can be built from it. | `verifier/observations.py` (`REQUIRED_META`), `tests/fixtures/snapshots/missing-pods.json`, `tests/test_snapshots.py` (`test_an_incomplete_fixture_turns_every_check_inconclusive`) |

## Declared gaps

- The CI workflow is written, passes `actionlint` and is asserted by `tests/test_workflow.py`. It has not been executed, because running it needs a root `.github/workflows/` file that this branch may not carry (R10, D61).
- The repeatability control is N=2. The README states that number instead of implying a larger sample.
- LIVE ran on one cluster shape (kind v1.37.0, Docker 29.9.3, 2026-11-16). Another environment is a new observation, not a rerun of this one.
