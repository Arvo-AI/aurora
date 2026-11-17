# Remediation verifier, aurora-lab scenario 12

In this incident, documented at [scenario 12, commit 4edcd9df](https://github.com/Arvo-AI/aurora-lab/tree/4edcd9dfbc48cbd9ca8ea031e429832cf72842d0/scenarios/12-two-faults-oom-and-selector) in `scenario.yaml#L3` and `#L17-L18`, the shop stops serving items and the alert names only one of the two faults behind it. Both live in one namespace: a worker crash-looping at `BATCH_SIZE=2500` against a 192Mi limit, and a frontend Service selecting `app=frontend,tier=web`, which no pod carries, so it has no endpoint and every request fails. The mechanisms are in `break.sh`; the conditions the lab checks are in `check.sh#L1-L2`.

Aurora's [published report](https://www.aurorasre.ai/blog/aurora-lab-report-kubernetes-19-failures) records why this matters: 0 of 2 runs on this scenario, and in run r1 the frontend was called healthy from pod status alone while its Service had no endpoint. Kubernetes tooling such as K8sGPT detects the crash loop; none of it verifies that a remediation closed both faults. That is the gap this repository fills: it reads the incident contract, runs six checks against a fix, and attaches the result as evidence. The frontend stays down while the selector is wrong, so `partial_worker_only` is rejected on `service_has_endpoints`, `endpoints_ready` and `http_expected_response`.

> Este verificador comprueba las condiciones declaradas en el contrato del incidente. No demuestra que no exista ningún otro defecto, y no reemplaza el juicio humano ni la revisión asesora de Aurora.
>
> El veredicto es advisory. Se adjunta como evidencia al hilo del incidente o al PR de remediación, pero no bloquea el merge. Aurora es un revisor asesor por diseño, y este verificador respeta esa filosofía.

The verdict is advisory evidence for a human reviewer: it is attached to the incident or the pull request, it never blocks a merge, and no branch protection lists it as a required check.

## Why now

Remediation is becoming machine-checkable. In two years an incident will hand an agent a contract with the conditions that define "closed", and the agent will propose a fix against them; the reviewer will read a verifier report, not a wall of logs. Aurora already diagnoses well, and what it does not do today is attach the verification of the remediation to the incident thread and to the pull request, as an advisor. This repository is that step for one scenario: the contract is data, and the six checks decide the fix, not the alert.

## Run it

```bash
make install
make doctor
make verify-snapshot
```

`make verify-snapshot` takes 0.6 s, prints the table below and writes `out/report.json` and `out/report.md`. It needs no cluster, network or credentials. `make ci` adds lint, typecheck and the 498 tests.

The LIVE path needs Docker, `kind` and `kubectl`:

```bash
make verify    # about 15 minutes, six variants in a kind cluster
make capture   # maintenance: rewrites fixtures/snapshots, never part of a review
```

`make verify` clones and checks out the pinned lab in `.cache/aurora-lab` first, so it only needs those three tools.

## What it decides

| Variant | Fix | Verdict within the contract | Checks |
|---|---|---|---|
| baseline | none | reproduction CONFIRMED | 0/6 |
| full | remove the selector key, then `BATCH_SIZE=500` | VERIFIED_WITHIN_CONTRACT | 6/6 |
| p1_raise_limit | remove the selector key, then memory limit 384Mi | VERIFIED_WITHIN_CONTRACT | 6/6 |
| partial_worker_only | `BATCH_SIZE=500` only | REJECTED | 3/6 |
| partial_frontend_only | selector only | REJECTED | 3/6 |
| noop | nothing | REJECTED | 0/6 |

The states are `VERIFIED_WITHIN_CONTRACT` (six PASS), `REJECTED` (any FAIL), `INCONCLUSIVE` (missing or ambiguous evidence) and `EXECUTION_ERROR` (a command or a timeout failed). The three rejected variants are the point: a healthy worker with a Service that has no endpoint is not a closed incident.

## The six checks

Three read the worker (no `OOMKilled`, no restart or pod replacement between captures, two processed batches at the Deployment's batch size) and three read the Service (an address, a ready address, three 200s with the expected items). A check that passes on the baseline is reported as weak.

## Provenance

Every fixture is a LIVE capture from `aurora-lab` at `4edcd9df` on 2026-11-16, with `lab_commit`, `variant`, `captured_at` and `provenance: LIVE`; one missing field makes it INCONCLUSIVE. Results are `LIVE` (from the cluster) or `SNAPSHOT` (replaying a fixture); `RECORDED` is not produced. `contracts/PROVENANCE.md` maps every value to a path and a line, and `make verify` reproduced the same six verdicts with 0 execution errors.

## Controls

`tests/test_controls.py` runs the four. N1 to N3 are rejected, so false acceptance is 0 of 3; P1 is accepted, so false rejection is 0 of 1; C1 mutates the baseline once per check and flips 6 of 6, leaving 0 of 6 weak checks; C2 repeats the baseline and the full fix from a reset, 3 of 3, with the same per-check statuses.

## What it does not do

- It does not choose a fix. It decides the fix it is given.
- It is not a merge gate. No branch protection should require it.
- It does not look for faults outside the contract, and it does not extend the 60 second window.
- It does not call a language model, a browser or a hosted service.
- It does not prove the absence of other defects.

## Limitations

- One scenario, one contract. `REMEDIATION_VERIFIER_CONTRACT` points the same code at another contract.
- LIVE ran on kind v1.37.0 with Docker 29.9.3 on 2026-11-16. Another cluster may pace rollouts and restarts differently.
- The verifier assumes exclusive use of the lab cluster. Two runs on the same kind cluster interfere, and a concurrent capture can reset it mid-window.
- The repeatability control is N=3, a repeat and not a statistical sample.
- The HTTP check runs inside the cluster through `kubectl exec deployment/loadgen`, so it needs that pod.

## Next step

The step after this repository is in Aurora: attach the report to a remediation pull request, with the conditions that hold, the ones that fail and the evidence for each, as an advisor that never blocks the merge. The success criterion is a reviewer who decides in under five minutes without rerunning the lab, with every claim backed by a check and a source. A second scenario is a new contract file, because the checks and the report take a scenario as data.

## Sources

- Contract `contracts/scenario_12.yaml`, schema `contracts/schema.json`, provenance `contracts/PROVENANCE.md`, decisions `contracts/DECISIONS.md`, audit `contracts/AUDIT.md`.
- Scenario, alert and fixes at `4edcd9df`: `https://github.com/Arvo-AI/aurora-lab/tree/4edcd9dfbc48cbd9ca8ea031e429832cf72842d0/scenarios/12-two-faults-oom-and-selector` (`scenario.yaml#L3`, `#L17-L18`); the 244MiB measurement `scenarios/12-two-faults-oom-and-selector/README.md#L160`; the worker log line `app/shop.yaml#L73`; the HTTP route `app/shop.yaml#L256-L257`.
- Fork base `kpnaranj/aurora` at `12c67cffd45c6b0b8c5da1d2e160a945e2936f49`.
