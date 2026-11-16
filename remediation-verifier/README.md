# Remediation verifier, aurora-lab scenario 12

In this incident the shop stops serving items, and the alert names only one of the two faults behind it. Scenario `12-two-faults-oom-and-selector` puts both in one namespace. The alerted fault: the worker crash-loops because `BATCH_SIZE` went from 500 to 2500 against a 192Mi limit. The silent one: the frontend Service selects `app=frontend,tier=web`, no pod carries `tier=web`, so the Service has no endpoint and every request fails. Both are documented in the scenario (`scenario.yaml#L3`, `#L17-L18`) at [commit 4edcd9df](https://github.com/Arvo-AI/aurora-lab/tree/4edcd9dfbc48cbd9ca8ea031e429832cf72842d0/scenarios/12-two-faults-oom-and-selector).

Kubernetes tooling such as K8sGPT detects the crash loop. None of it verifies that a remediation closed both faults, and that is the gap this repository fills: it reads the incident contract, runs six checks against a fix, and attaches the result as evidence. A fix for the worker alone leaves the shop down, which is why `partial_worker_only` is rejected on the three frontend checks.

> Este verificador comprueba las condiciones declaradas en el contrato del incidente. No demuestra que no exista ningún otro defecto, y no reemplaza el juicio humano ni la revisión asesora de Aurora.
>
> El veredicto es advisory. Se adjunta como evidencia al hilo del incidente o al PR de remediación, pero no bloquea el merge. Aurora es un revisor asesor por diseño, y este verificador respeta esa filosofía.

The verdict is advisory evidence for a human reviewer: it is attached to the incident or the pull request, it never blocks a merge, and no branch protection lists it as a required check.

## Why now

Remediation is becoming machine-checkable. In two years an incident will hand an agent a contract with the conditions that define "closed", and the agent will propose a fix against them; the reviewer will read a verifier report, not a wall of logs. Aurora already diagnoses well, and what it does not do today is attach the verification of the remediation to the incident thread and to the pull request, as an advisor. This repository is that step for one scenario: the contract is data, and the six checks decide the fix, not the alert.

## Run it

```bash
cd remediation-verifier
make install
make doctor
make verify-snapshot
```

`make verify-snapshot` takes about 0.6 s, prints the table below and writes `out/report.json` and `out/report.md`. It reads the committed fixtures, so it needs no cluster, no network and no credentials. `make ci` runs lint, typecheck, the test suite and that snapshot gate. `make help` lists the rest.

The LIVE path needs Docker, `kind` and `kubectl`:

```bash
make verify    # about 15 minutes, six variants against a kind cluster
make capture   # the same run, and rewrites fixtures/snapshots
```

`make lab-pin` clones `aurora-lab` at `4edcd9df` into `.cache/aurora-lab` and checks out the commit named in `verifier/config.py`. `make lab-up` creates the cluster when it is missing. Both run first, so `make verify` is enough once those three tools are installed.

## What it decides

| Variant | Fix | Verdict within the contract | Checks |
|---|---|---|---|
| baseline | none | reproduction CONFIRMED | 0/6 |
| full | remove the selector key, then `BATCH_SIZE=500` | VERIFIED_WITHIN_CONTRACT | 6/6 |
| p1_raise_limit | remove the selector key, then memory limit 384Mi | VERIFIED_WITHIN_CONTRACT | 6/6 |
| partial_worker_only | `BATCH_SIZE=500` only | REJECTED | 3/6 |
| partial_frontend_only | selector only | REJECTED | 3/6 |
| noop | nothing | REJECTED | 0/6 |

The four states are `VERIFIED_WITHIN_CONTRACT` (six PASS with evidence), `REJECTED` (any FAIL), `INCONCLUSIVE` (missing or ambiguous evidence) and `EXECUTION_ERROR` (a command, the cluster or a timeout failed).

The alert names the worker only. The second fault leaves the frontend Service with no endpoint, and a fix for the worker alone keeps the shop down. The three rejected variants are that case: a healthy worker is not a closed incident.

## The six checks

| Fault | Check | Passes when |
|---|---|---|
| worker-oom | `no_oomkilled_in_window` | no worker pod carries `lastState.terminated.reason: OOMKilled`, and none appears during the window |
| worker-oom | `restart_count_not_increasing` | 0 restarts at the start and at the end, on the same pod UID |
| worker-oom | `processing_evidence` | at least 2 lines `processed batch of <BATCH_SIZE> items`, with the batch size read from the Deployment |
| frontend-selector | `service_has_endpoints` | the frontend EndpointSlice has at least one address |
| frontend-selector | `endpoints_ready` | at least one address has `conditions.ready: true` |
| frontend-selector | `http_expected_response` | 3 requests through the Service return 200 and `items` equals `["kettle","mug","teapot"]` |

A check that passes on the baseline is a weak check, and the verdict reports it instead of counting it as detection.

## Provenance

Every fixture is a LIVE capture from `aurora-lab` commit `4edcd9dfbc48cbd9ca8ea031e429832cf72842d0`, taken on 2026-11-16 between `01:03:39Z` and `01:39:46Z`. Each snapshot carries `lab_commit`, `variant`, `captured_at` and `provenance: LIVE`. A snapshot missing any of them becomes INCONCLUSIVE, never a positive. Every result is labelled `LIVE` or `SNAPSHOT`: `LIVE` means the capture came from the cluster, `SNAPSHOT` means it replays a committed fixture. `RECORDED` would be a stored trace replayed the same way, and this repository does not produce that mode. `contracts/PROVENANCE.md` maps every fixture, fix and check to a path and a line, and `contracts/DECISIONS.md` records the choices that are ours. `make verify` produced the same six verdicts against the lab on 2026-11-16 at `02:07:34Z`, with 0 execution errors.

## Controls

`tests/test_controls.py` runs the four: N1 to N3 are rejected, so false acceptance is 0 of 3; P1 is accepted, so false rejection is 0 of 1; C1 mutates the baseline observation once per check and flips 6 of 6, leaving 0 of 6 weak checks; C2 repeats the baseline and the full fix from a reset, 2 of 2, and the same per-check statuses come back.

## What it does not do

- It does not choose a fix. It decides the fix it is given.
- It is not a merge gate. No branch protection should require it.
- It does not look for faults outside the contract, and it does not extend the 60 second window.
- It does not call a language model, a browser or a hosted service.
- It does not prove the absence of other defects.

## Limitations

- One scenario, one contract. `REMEDIATION_VERIFIER_CONTRACT` points the same code at another contract, and `contracts/scenario_12.yaml` is the only one frozen here.
- LIVE ran on kind v1.37.0 with Docker 29.9.3 on 2026-11-16. Another cluster may pace rollouts and restarts differently.
- The repeatability control uses N=2. That is a repeat, not a statistical sample.
- The HTTP check runs inside the cluster through `kubectl exec deployment/loadgen`, so it needs that pod.

## Next step

The step after this repository is in Aurora: attach the report to a remediation pull request, with the conditions that hold, the ones that fail and the evidence for each, as an advisor that never blocks the merge. The success criterion is a reviewer who decides in under five minutes without rerunning the lab, with every claim in the report backed by a check and a source. A second scenario is then a new contract file, because the contract, the checks and the report already take a scenario as data.

## Sources

- Contract `contracts/scenario_12.yaml`, schema `contracts/schema.json`, audit `contracts/AUDIT.md`.
- Scenario at `4edcd9df`: `https://github.com/Arvo-AI/aurora-lab/tree/4edcd9dfbc48cbd9ca8ea031e429832cf72842d0/scenarios/12-two-faults-oom-and-selector`
- Alert `scenarios/12-two-faults-oom-and-selector/scenario.yaml#L3`; accepted fixes `#L17-L18`; the 244MiB measurement `scenarios/12-two-faults-oom-and-selector/README.md#L160`; the worker log line `app/shop.yaml#L73`; the HTTP route `app/shop.yaml#L256-L257`.
- Fork base `kpnaranj/aurora` at `12c67cffd45c6b0b8c5da1d2e160a945e2936f49`.
