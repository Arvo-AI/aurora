# AGENTS.md — Aurora Incident-to-Remediation Verifier

> **Placement:** copy this file to `remediation-verifier/AGENTS.md` in the Aurora fork.
> Do **not** modify the Aurora repo's root `AGENTS.md` or `CLAUDE.md`; they already exist, and R10 forbids changes outside `remediation-verifier/`.

## 1. Read first

In this order:

1. `SETUP_PLAN.md` — setup, cases, CI/CD, submission.
2. `take_home_one_pager.md` — the evaluator's rules.
3. `JUEZ v4.md` — the scoring rubric and R1–R11 disqualifiers.
4. `GUIA PROYECTO v4.md` — the project plan.

When the guide and the JUEZ conflict, the JUEZ wins. When the JUEZ and the one-pager conflict, the one-pager's rules win.

`SETUP_PLAN.md`, `GUIA PROYECTO v4.md`, `JUEZ v4.md` and `take_home_one_pager.md` are **internal workspace files**. They are not committed. If you are reading this inside the fork, the committed `README.md` and `contracts/` are the operative sources.

## 2. Mission

Build a **deterministic, advisory remediation verifier** for `aurora-lab` scenario `12-two-faults-oom-and-selector`.

> Verify the fix against the incident, not just the alert.

It is **not**:

- an LLM agent;
- a merge gate;
- a replacement for human review;
- a browser or E2E test runner;
- a cloud/Slack/Datadog/PagerDuty integration.

## 3. Hard rules — never violate

| # | Rule |
|---|---|
| R1 | Never present a SNAPSHOT or RECORDED result as LIVE. Every result carries a provenance label. |
| R2 | Never block a merge or present the verifier as a required gate. |
| R3 | Never say "fixed" or "solved" without "within the contract". |
| R4 | Every fix and fixture has a documented source. No invented values. |
| R5 | Never accept a fix because it matches a hard-coded test value. Accept it because the checks pass. |
| R6 | The oracle never appears in `agent_view()`, in agent-visible logs, or in agent-visible reports. |
| R7 | Every Aurora claim has a public URL or a repo path + commit + line. |
| R8 | No HolmesGPT, Aurora or competitor numbers in the README or PR argument. |
| R9 | Every metric has a denominator. |
| R10 | Only `remediation-verifier/` changes. Never modify `scenarios/`, `runner/`, `lab.sh`, `app/`, or root files. |
| R11 | A snapshot missing any required metadata produces `INCONCLUSIVE`, never a positive. |

## 4. Repo boundaries

**Allowed:**

```
remediation-verifier/
  AGENTS.md
  README.md
  Makefile
  pyproject.toml
  contracts/
  verifier/
  fixtures/
  tests/
  ci/
```

**Forbidden:**

- Any change to `scenarios/`, `runner/`, `lab.sh`, `app/` in `aurora-lab`.
- Any root file in `aurora`: `.github/`, `AGENTS.md`, `CLAUDE.md`, `Makefile`, `README.md`, `server/`, `client/`, etc.
- Committing `GUIA PROYECTO v4.md`, `JUEZ v4.md`, `take_home_one_pager.*`, `SETUP_PLAN.md`.
- Committing `.env`, kubeconfigs, tokens, secrets, cloud credentials.
- Committing `out/`, `.venv/`, `.cache/`, `.pytest_cache/`, `__pycache__/`.

**Before every push:**

```bash
git diff --stat main...HEAD
# Every path must start with remediation-verifier/
```

**Git rules — the candidate pushes.** The agent may create local commits. It must never run `git push`, `git push --force`, `gh pr create`, or any command that publishes to a remote. Every commit is reviewed on the machine before it leaves it.

## 5. Source discipline

- Read `scenario.yaml`, `break.sh`, `check.sh`, `README.md` (runbook) and `app/shop.yaml` at the pinned commit.
- Pin `aurora-lab` to `4edcd9dfbc48cbd9ca8ea031e429832cf72842d0` (or a later commit you have actually read).
- Record the fork base SHA in the contract and README.
- If a value is unknown, write `PENDIENTE` and ask. Do not guess.
- Every fixture maps to a row in `contracts/PROVENANCE.md`.
- A textual quote from a trace needs path, commit and line.

## 6. Architecture

| File | Responsibility |
|---|---|
| `verifier/contract.py` | Load and validate the contract; expose `agent_view()` and `oracle_view()`. |
| `verifier/observations.py` | Normalize LIVE and SNAPSHOT observations into one schema. |
| `verifier/checks.py` | Six independent checks; each returns PASS/FAIL/INCONCLUSIVE with evidence. |
| `verifier/runner.py` | `reset → break → baseline → fix → wait rollout → observe → checks`. |
| `verifier/decision.py` | Four verdict states + `reproduction_status`; identical for LIVE and SNAPSHOT. |
| `verifier/report.py` | JSON and Markdown with per-check evidence and provenance. |
| `verifier/cli.py` | `--mode snapshot|live|capture`, `--out`, `--lab`. |

**Rules:**

- LIVE and SNAPSHOT share `observations.py` and `decision.py`. SNAPSHOT never calls Kubernetes.
- No LLM calls, no browser, no external services.
- Shell out to `kubectl` with explicit timeouts; do not use a heavy Kubernetes client.
- Python 3.11+, `pathlib`, type hints, dataclasses. Runtime dependency: `PyYAML`. Test dependency: `pytest`. Dev-only and pinned: `ruff`, `mypy` (justified in `contracts/DECISIONS.md` D12). Nothing else.
- Deterministic output: sorted keys, stable ordering, no volatile fields in golden tests except `captured_at`.

## 7. The six checks

| Fault | Check | Pass |
|---|---|---|
| worker-oom | `no_oomkilled_in_window` | No current worker pod has `lastState.terminated.reason: OOMKilled`; no new OOMKilled in the window. |
| worker-oom | `restart_count_not_increasing` | 0 restarts at window start and 0 at window end; no pod replacement during the window. A non-zero start fails, so the check is fault-enforcing at baseline. |
| worker-oom | `processing_evidence` | ≥2 lines `processed batch of <current BATCH_SIZE> items` in `deploy/worker` during the window. Read `BATCH_SIZE` from the Deployment (500 for the full fix, 2500 for P1). |
| frontend-selector | `service_has_endpoints` | Frontend EndpointSlice has ≥1 address. |
| frontend-selector | `endpoints_ready` | ≥1 endpoint has `conditions.ready: true`. |
| frontend-selector | `http_expected_response` | 3 requests through the Service return 200 and `items == ["kettle","mug","teapot"]`. |

**Baseline:** all six FAIL. **Full fix:** all six PASS. A check that passes at baseline is a weak check (C1) and must be reported.

## 8. Evidence rules

Every result includes:

- `mode`: `LIVE`, `SNAPSHOT` or `RECORDED`;
- `check_id`;
- `status`: `PASS`, `FAIL`, `INCONCLUSIVE`;
- `evidence`: the raw observation, command, output, timestamp;
- `source`: contract path + commit (and line where textual).

No positive verdict without evidence. No snapshot positive without complete metadata:

```
lab commit, variant, pods and states, restart counts,
services and endpoints, HTTP route/status/body,
processing evidence, capture date, provenance label
```

Missing any of these → `INCONCLUSIVE`.

## 9. Decision rules

- Baseline → `reproduction_status ∈ {CONFIRMED, NOT_CONFIRMED}`. No remediation verdict.
- Remediation → `VERIFIED_WITHIN_CONTRACT` only if all six checks PASS with evidence.
- Any FAIL → `REJECTED`.
- Missing/ambiguous evidence → `INCONCLUSIVE`.
- Command/cluster/timeout failure → `EXECUTION_ERROR`.
- Baseline not confirmed → no positive verdict for any variant.
- A variant's pre-fix state not confirmed → no positive verdict for that variant.
- Snapshot missing metadata → `INCONCLUSIVE`.
- The verdict never blocks a merge. It is advisory.

## 10. Language rules

- Say "within the contract" whenever you say a fix passed.
- Say what the verifier does **not** do with the same clarity as what it does.
- The README must contain the JUEZ §8 scope sentence **verbatim** (Spanish). Do not paraphrase it.
- The README declares the verdict advisory and non-blocking.
- No competitor numbers. No unverified Aurora claims. No "roadmap" language.
- Cite exact URLs, commits, paths and lines.

## 11. Workflow

Work one case at a time, in the order in `SETUP_PLAN.md` §6.

**Before coding a case:**

1. Re-read the relevant contract/provenance rows.
2. Write or update the test first.
3. Confirm the fixture has a source.

**After coding a case:**

```bash
cd remediation-verifier
source .venv/bin/activate
make test
make verify-snapshot
git diff --stat main...HEAD
```

Update the case tracker with status and evidence path.

**Commit messages:** Conventional Commits, as the repo history does (`fix(prediscovery): ... (#821)`), with the case tag for traceability:

```
chore(verifier): add quality gates and make targets (case 0.5)
feat(verifier): freeze scenario 12 contract and provenance (case 1)
feat(verifier): add six independent checks (case 4)
test(verifier): add snapshot fixtures for five variants (case 7)
```

## 12. Testing rules

- `pytest` must pass in a clean venv.
- The view separation test must prove `agent_view()` contains only the alert.
- N1 (worker-only), N2 (frontend-only), N3 (no-op) → all `REJECTED`; false acceptance 0/3.
- P1 (raise memory limit + remove `tier`) → `VERIFIED_WITHIN_CONTRACT`; false rejection 0/1 if P1 applies. P1 keeps `BATCH_SIZE=2500`, so processing evidence must match 2500.
- C1: a check that passes at baseline is reported as weak.
- C2: the same fix from `reset` produces the same verdict.
- N≥3 on baseline and full fix if time; otherwise report the real N.
- Fault detection rate: 6/6 at baseline. Weak-check rate: 0/6.

## 13. Make targets

| Target | Requirement |
|---|---|
| `make doctor` | Report Python, PyYAML, dev tools, `kubectl`, `kind`, Docker and the lab checkout; name the supported mode. |
| `make test` | pytest. |
| `make selfcheck` | Clone the branch into a temp directory, install from scratch and run `make ci` there. |
| `make lint` | `ruff check` + `ruff format --check`. |
| `make lint-ci` | `actionlint ci/github-actions-verify.yml` (local only; not in `make ci`). |
| `make typecheck` | `mypy verifier`. |
| `make verify-snapshot` | No Kind, no network, <60 s, prints the five-variant table. |
| `make verify` | Kind + pinned lab; five variants; LIVE evidence. |
| `make capture` | LIVE run that writes the five snapshots. |
| `make ci` | lint + typecheck + test + verify-snapshot. |
| `make lab-pin` | Clone/checkout the pinned lab if missing. |
| `make lab-up` | `lab.sh up` (idempotent). |
| `make clean` | Remove generated artifacts. |

## 14. CI/CD rules

- Workflow template lives at `remediation-verifier/ci/github-actions-verify.yml`.
- Do not add a root `.github/workflows/` file to the PR branch. Run CI from a separate `ci` branch or public CI repo; link the run in the PR description.
- `permissions: contents: read`; no secrets; no `pull_request_target`.
- `timeout-minutes` on every job; `concurrency` to cancel superseded runs.
- Upload `out/` as an artifact.
- CD means the evidence bundle, not a production deployment.

## 15. Definition of done

- `make verify-snapshot` <60 s in a clean clone.
- `make verify` works or is declared not tested (no fake LIVE).
- `pytest` green in a clean venv.
- Five variants with per-check evidence and provenance.
- Baseline `CONFIRMED`; full fix `VERIFIED_WITHIN_CONTRACT`; partials and no-op `REJECTED`.
- False acceptance 0/3.
- `agent_view()` separation test passes.
- README one page, exact scope sentence, advisory, limitations, next step.
- `git diff --stat main...HEAD` only `remediation-verifier/`.
- Draft PR against `main` with the exact title:
  `[candidate][B] Remediation verification against the full incident contract (scenario 12)`

## 16. If you are unsure

Ask. Do not invent a fix, a check, a window, an HTTP route, an expected response or a source. The contract is the foundation; a guessed value wastes the build.
