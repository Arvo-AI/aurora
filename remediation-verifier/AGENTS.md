# AGENTS.md: Aurora Incident-to-Remediation Verifier

> **Placement:** copy this file to `remediation-verifier/AGENTS.md` in the Aurora fork.
> Do **not** modify the Aurora repo's root `AGENTS.md` or `CLAUDE.md`; they already exist, and R10 forbids changes outside `remediation-verifier/`.

## 1. Read first

In this order:

1. `SETUP_PLAN.md`: setup, cases, CI/CD, submission.
2. `take_home_one_pager.md`: the evaluator's rules.
3. `JUEZ v4.md`: the scoring rubric and R1-R11 disqualifiers.
4. `GUIA PROYECTO v4.md`: the project plan.

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

## 3. Hard rules (never violate)

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

**Git rules: the candidate owns the repository.**

- The agent only creates and edits files in the working tree.
- The agent never runs a command that writes to the repository: no `git add`, no `git commit`, no `git push`, no `git reset`, no `git stash`, no `git checkout`, no branch or tag creation, no rewrite of any kind.
- Read-only inspection is allowed: `git status`, `git diff`, `git log`, `git show`.
- The candidate reviews the working tree, writes every commit and performs every push. Nothing changes in the repository without them.

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

### 10.1 Typography

Sources: `Wikipedia:Signs of AI writing`, section 13.5 "Overuse of em dashes"
and its Style section,
https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing. That page reports
that spaced em dashes are one of the most reproduced LLM tells, that a July 2026
study found only one model family using them more than professional writers, and
that curly quotation marks are a listed style tell. `Wikipedia:Programming style`
is the source for the layout rules in 10.3.

| # | Rule | Use instead |
|---|---|---|
| W1 | No em dash (U+2014) or en dash (U+2013) in prose, chat or commit messages. | A colon, a comma, parentheses, or a second sentence. Use a hyphen for ranges: 2022-2026. |
| W2 | No curly quotation marks or apostrophes (U+2018, U+2019, U+201C, U+201D). | Straight `'` and `"`. |
| W3 | No ellipsis character (U+2026). | Three periods. |
| W4 | No emoji, in code, docs, commits or chat. | A word. |
| W5 | Sentence case in headings and titles. | "How the checks work", not "How The Checks Work". |
| W6 | At most one bold run per paragraph, and only for a label. | Plain text. |
| W7 | No horizontal rule used as decoration. | A heading. |

### 10.2 Prose patterns to avoid

Each row cites the section of `Wikipedia:Signs of AI writing` that documents it.

| # | Pattern | Section | Write instead |
|---|---|---|---|
| W8 | "not just X, but Y" and "not X, but Y". | 3.3 Negative parallelisms | State Y. |
| W9 | Three-item lists that exist for rhythm rather than content. | 3.5 Rule of three | List what actually exists. |
| W10 | "serves as", "stands as", "represents", "acts as". | 3.2 Avoidance of basic copulatives | "is". |
| W11 | Trailing "-ing" clauses that assign significance ("..., highlighting its importance"). | 2.3 Superficial analyses | Delete the clause, or state the fact. |
| W12 | "delve", "leverage", "robust", "seamless", "crucial", "pivotal", "underscore", "foster". | 3.1 AI vocabulary density | The plain word. |

### 10.3 Code and document layout

| # | Rule |
|---|---|
| W13 | Python: four spaces, never tabs. `ruff format` is the authority; do not hand-align or hand-wrap. |
| W14 | No vertical alignment of `=` or `:` across lines. The next rename breaks it. |
| W15 | No trailing whitespace. Exactly one newline at end of file. LF line endings in every committed file. |
| W16 | ASCII only in `.py`, `.yml`, `.yaml`, `.toml`, `Makefile` and shell. Prose may use accents; it still follows W1 to W7. |
| W17 | Markdown: one space after the list marker, two spaces per nesting level, one marker style per list, one blank line between blocks. |
| W18 | Indentation must be stable under `ruff format --check` and `make lint`. If a human has to guess the depth, it is wrong. |

### 10.4 The check

Run this before every commit. It must print nothing.

```bash
grep -rnP '\x{2014}|\x{2013}|\x{2018}|\x{2019}|\x{201C}|\x{201D}|\x{2026}' \
  --include='*.py' --include='*.md' --include='*.yml' --include='*.yaml' \
  --include='Makefile' .
```

The patterns are written as escapes so this file passes its own check. `grep -P`
is GNU grep; the project runs in WSL or Linux, where it is available.

### 10.5 Why these are tables and not advice

Guidance written for one assistant ("write naturally", "avoid sounding like a
machine") depends on taste, and taste does not transfer between models. A rule
that can be grepped transfers. If a rule here cannot be checked, it does not
belong here.

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
