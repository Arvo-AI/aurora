# Decisions

This file records the choices a reader could disagree with. A value that comes from `aurora-lab` lives in `scenario_12.yaml` and `PROVENANCE.md`; the rows here are the decisions that are ours, frozen before code depends on them. Changing one is a new decision with a new number, never a silent edit.

| ID | Decision | Frozen value | Why |
|---|---|---|---|
| D1 | Submission repo, branch and pull request | Fork of `Arvo-AI/aurora` under `kpnaranj/aurora`, branch `kpnaranj/remediation-verifier`, draft pull request against `main` | The one-pager asks for a fork, a branch and a draft pull request. Every change stays inside `remediation-verifier/`, which is R10. |
| D2 | Observation window | 60 seconds | The runbook watches two batch cycles of five seconds (`scenarios/12-two-faults-oom-and-selector/README.md#L202`). 60 seconds leaves room for a rollout and one restart inside the same window. |
| D3 | Processing evidence | At least 2 lines matching `processed batch of <current BATCH_SIZE> items`, batch size read from `deploy/worker` | The pattern is the worker's own line (`app/shop.yaml#L73`), the count is the runbook's two cycles, and reading the batch size from the Deployment keeps a fix that leaves 2500 valid. |
| D4 | HTTP probe | 3 GET requests to `http://frontend.shop.svc.cluster.local/api/items`, two seconds apart, five second timeout, status 200, body `items` equal to the list in `app/shop.yaml#L48` | One request can answer from a single lucky pod. Three requests still fit inside the window and follow the client path the load generator uses. |
| D5 | Full fix | Remove `/spec/selector/tier` with a JSON patch, then `kubectl -n shop set env deploy/worker BATCH_SIZE=500`, frontend first | These are the runbook's own commands (`scenarios/12-two-faults-oom-and-selector/README.md#L164`, `#L170`, `#L180`). A strategic merge patch keeps `tier`, and so does re-applying the manifest. |
| D6 | Partial fixes and no-op | The inverse of each `break.sh` line, and the deliberate absence of a change | Derived from the mechanism that injected each fault (`scenarios/12-two-faults-oom-and-selector/break.sh#L1`, `#L2`), not invented. The no-op is the control that a familiar cluster cannot turn green. |
| D7 | Alternative fix (P1) | Raise `resources.limits.memory` above 244 MiB and remove `/spec/selector/tier`, keeping `BATCH_SIZE=2500` | `scenarios/12-two-faults-oom-and-selector/scenario.yaml#L18` allows raising the limit with the reason stated. 244 MiB is 2500 items of 100 KiB (`app/shop.yaml#L72`). Optional, built in case 8 if the clock allows. |
| D8 | Default mode | `snapshot` | It needs no cluster, no network and no credentials. `make doctor` reported the docker daemon as WARN on 2026-11-15, so SNAPSHOT is the mode this machine can always run. |
| D9 | LIVE mode | Implemented, and declared not tested until a Kind run succeeds | The JUEZ allows declaring LIVE untested. A declared gap costs evidence points; a fake LIVE run is a disqualifier (R1). |
| D10 | CI topology | Workflow template at `ci/github-actions-verify.yml`, run from a separate `ci` branch | A root `.github/workflows/` file would break R10. The template is copied to a branch that publishes only CI. |
| D11 | Restart strictness | 0 restarts at window start and 0 at window end, on the same pod UID | A plain not-increasing check passes at baseline while a CrashLoopBackOff backoff is longer than the window. Requiring a clean pod makes the check fail at baseline and pass for every valid remediation. |
| D12 | Dev-only tooling | `ruff 0.17.5`, `mypy 2.4.1`, `pytest 9.2.0`, runtime `PyYAML 6.0.3` | The runtime dependency is PyYAML. `ruff` and `mypy` are never imported by `verifier/` and do not ship, and `pytest` is the test runner the assessment names. These versions were resolved by `make install` on 2026-11-15 with Python 3.12.3, so they are recorded, not guessed. `pyproject.toml` keeps its declared bounds. |
| D13 | Coverage and hooks | No `pytest-cov`, no `pre-commit` | A coverage percentage says nothing about six checks, and a hook can be skipped. `make ci` and the workflow are the gates. |
| D14 | Commits and branch name | Conventional Commits as `type(verifier): summary (case N)`, branch `kpnaranj/remediation-verifier` | It matches the repository history, for example `fix(prediscovery): schedule it only when a provider is newly connected (#821)`. The branch name deviates from `CONTRIBUTING.md#L148-L167`, which asks for a lowercase prefix, so the deviation is written here instead of hidden. |
| D15 | Test layout | `tests/test_<subject>.py`, classes `Test<Subject>`, sentence-style test names | It mirrors `server/tests/services/test_change_gating_adapter.py` at the fork base, so a reader of Aurora's tests finds the same shape here. |
| D16 | Style contract | `from __future__ import annotations`, a module `LOGGER`, frozen dataclasses, constants with a unit suffix such as `COMMAND_TIMEOUT_S` | Taken from `server/chat/backend/agent/access/mode_access_controller.py` at the fork base, and already used by `verifier/doctor.py`. |
| D17 | README content | Opening Aurora citation with its URL, the five-variant table, the two-year SDLC paragraph, and the JUEZ section 8 sentence verbatim | The JUEZ asks for all four explicitly, in criteria 1.1 and 1.5 and in its evidence checklist. |
| D18 | No web UI | Markdown and JSON reports only | R10 and D12 keep the surface small. The evidence an incident thread can carry is a JSON file plus a Markdown report. |
| D19 | Contract shape | Deltas from the planning draft: `observation.window_seconds` at the oracle level, a `source` on every check and fix, request parameters instead of one route string, and no `provenance` block inside the YAML | One window applies to the whole run. The per-check `source` is what the report prints. The provenance table lives in `PROVENANCE.md` only, because a second machine-readable copy drifts. |
| D20 | Provenance rule | Every cited path and line anchor appears in `PROVENANCE.md`, and one pinned commit applies to every row | This is R4 and R7. A row without a source is not used, and a fixture that cannot be traced is deleted instead of kept. |
| D21 | Window opens late | The runner waits for `kubectl rollout status deploy/worker` and a non-empty frontend EndpointSlice before the window starts | A stale OOMKilled pod, or an empty slice during a rollout, would report a valid fix as a failure. |
| D22 | Baseline gates every verdict | No variant can be positive unless the baseline run confirmed both faults | A fix cannot be verified against an incident that did not reproduce in the same run. |
| D23 | Two endpoint checks, not one | `service_has_endpoints` reads the EndpointSlice addresses and `endpoints_ready` reads `conditions.ready` on those addresses | A pod's address joins the slice before the pod passes readiness, so a selector change that points at a pod still starting passes the first check and fails the second. Read together the two checks also name the failure: no address means the selector matches nothing, an unready address means the pod is not serving yet. |
| D24 | Code size limits | Module 300 lines soft and 400 hard, function 40 and 50, complexity 10, nesting 3, line length 100 | Six checks in one module stay readable while each one fits on a screen. `ruff` enforces the complexity at 10 (`C90`) and the line length at 100 (`E501`), so those two fail the build. Line counts and nesting are reviewed by hand because `ruff` does not measure them, and the soft limit is where a module is split before it reaches the hard one. |
| D25 | Configuration surface | `verifier/config.py` holds the paths, the encoding, the schema name and the lab pin; `REMEDIATION_VERIFIER_CONTRACT` overrides the contract path, and the schema is resolved as a sibling of whichever contract is loaded | A second scenario then costs a pair of files, not a code change. The pin has one definition and `doctor` imports it, so the machine check and the contract cannot disagree without a test failing. |
| D26 | Model and loader split | Frozen dataclasses in `verifier/model.py`; loading, validation and the two views in `verifier/contract.py` | D24 caps a module at 300 lines soft, and the ten types plus the loader reached 419. The split also lets later modules import the types without importing the YAML reader. |
| D27 | Views are a shape, not a filter | `AgentView` has one field, `alert`, and `agent_view()` builds its dictionary from it | R6 asks that the oracle never reach the agent. Copying the contract and deleting keys leaves the next added key exposed; a one-field type has nothing to delete. A test serializes the view and fails on oracle vocabulary, and a second test proves that detector catches a planted key. |
| D28 | Type stubs for PyYAML | A mypy override (`ignore_missing_imports` for `yaml`) instead of the `types-PyYAML` package | D12 allows `ruff` and `mypy` as dev tooling and nothing else. One module imports PyYAML and narrows the result to a mapping before anything else reads it, so the loss is confined to that call. |
| D29 | Structure raises, metadata values are reported | A section or field of the wrong JSON type raises `ObservationError`; a structurally valid value that cannot be used (a naive timestamp, a provenance label other than `LIVE`, an empty commit) is listed in `missing_metadata`, and `require_complete()` is the strict door that raises | R11 says a snapshot missing metadata produces INCONCLUSIVE, and aborting the run would leave no verdict at all. A boolean written as the string `"true"` is a corrupt document, so it fails loudly instead of becoming a silent `false`. |
| D30 | Four modules for the observation layer | `model.py` holds every frozen type; `observations.py` parses snapshots; `live.py` collects and normalizes kubectl output; `kubectl.py` wraps the binary and the HTTP probe | D24 caps a module at 300 lines and the schema plus both collectors reached 778. The split also makes "SNAPSHOT never calls Kubernetes" a property of the imports: nothing on the snapshot path imports `live.py` or `kubectl.py`. |
| D31 | Load generator logs | `kubectl -n shop logs deployment/loadgen --since=<window>s`, captured as supporting evidence and allowed to be empty | `app/shop.yaml#L236-L243` declares the deployment, and its tail shows what a user saw during the window. It is not required metadata, so a missing tail logs a warning instead of aborting a capture. |
| D32 | Snapshot schema artifact | `contracts/snapshot_schema.json`, checked by the same local validator as the contract, with no required sections | The plan asks for a snapshot schema. Requiring sections would turn a missing one into an exception, which contradicts R11: absence must produce INCONCLUSIVE. So the schema checks the type of every section that is present, and the parser still reports what is missing. |
| D33 | Timing is measured, never snapshotted | `from_live` times the pods, services, endpoints, http and logs phases, merges caller phases, and `snapshot_document` omits timing | A duration differs on every run, so writing it would break the byte-identical snapshot the tests need. The live report can still show how long each phase took. |

## Recorded environment

Measured on 2026-11-15 with `make doctor`, so the numbers below are observed rather than planned.

| Item | Value |
|---|---|
| Python | 3.12.3 |
| PyYAML | 6.0.3 |
| pytest | 9.2.0 |
| ruff | 0.17.5 |
| mypy | 2.4.1 |
| kubectl | v1.35.3 |
| kind | v0.33.0 |
| docker daemon | not reachable, so LIVE is unavailable on this machine (D9) |
| `aurora-lab` checkout | `4edcd9dfbc48`, equal to the pin in `scenario_12.yaml` |
| available mode | SNAPSHOT |

## Open items

- LIVE has not run. Until it does, no result in this project is labelled LIVE (R1, D9).
- The contract is frozen at the pin above. A later `aurora-lab` commit is a new contract revision, reviewed on its own.
