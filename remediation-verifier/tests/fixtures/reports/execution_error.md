# Remediation verification report

Advisory. This report is evidence for a human reviewer and never a merge gate.

- Scenario: `12-two-faults-oom-and-selector`
- Title: Two faults at once, worker OOM and a frontend Service selecting no pods; the alert covers only the worker
- Mode: `SNAPSHOT`
- Generated: `2026-11-15T21:05:00Z`
- Lab commit: `4edcd9dfbc48cbd9ca8ea031e429832cf72842d0`
- Fork commit: `12c67cffd45c6b0b8c5da1d2e160a945e2936f49`

## Metrics

| Metric | Value | Denominator |
|---|---|---|
| Fault detection | 6 failed | 6 checks |
| Weak checks | 0 passed | 6 checks |
| Verified | 0 | 1 remediation run |
| Rejected | 0 | 1 remediation run |
| Inconclusive | 0 | 1 remediation run |
| Execution errors | 1 | 1 remediation run |

## Variants

| Variant | Kind | Mode | Status | Reproduction | Passed |
|---|---|---|---|---|---|
| baseline | reproduction | SNAPSHOT | not applicable | CONFIRMED | 0/6 |
| full | remediation | SNAPSHOT | EXECUTION_ERROR | CONFIRMED | 0/0 |

### baseline

- Status: `not applicable`
- Reproduction status: `CONFIRMED`
- Mode: `SNAPSHOT`
- Captured: `2026-11-15T20:00:00Z`
- Reason: the baseline failed all 6 declared checks for worker-oom, frontend-selector

| Check | Fault | Status | Reason | Source |
|---|---|---|---|---|
| no_oomkilled_in_window | worker-oom | FAIL | FAIL fixture | scenarios/12-two-faults-oom-and-selector/check.sh#L1 |
| restart_count_not_increasing | worker-oom | FAIL | FAIL fixture | scenarios/12-two-faults-oom-and-selector/README.md#L202 |
| processing_evidence | worker-oom | FAIL | FAIL fixture | app/shop.yaml#L73 |
| service_has_endpoints | frontend-selector | FAIL | FAIL fixture | scenarios/12-two-faults-oom-and-selector/check.sh#L2 |
| endpoints_ready | frontend-selector | FAIL | FAIL fixture | scenarios/12-two-faults-oom-and-selector/check.sh#L2 |
| http_expected_response | frontend-selector | FAIL | FAIL fixture | app/shop.yaml#L256-L257 |

<details><summary>Evidence for no_oomkilled_in_window</summary>

```json
{
  "fixture": "no_oomkilled_in_window"
}
```

</details>

<details><summary>Evidence for restart_count_not_increasing</summary>

```json
{
  "fixture": "restart_count_not_increasing"
}
```

</details>

<details><summary>Evidence for processing_evidence</summary>

```json
{
  "fixture": "processing_evidence"
}
```

</details>

<details><summary>Evidence for service_has_endpoints</summary>

```json
{
  "fixture": "service_has_endpoints"
}
```

</details>

<details><summary>Evidence for endpoints_ready</summary>

```json
{
  "fixture": "endpoints_ready"
}
```

</details>

<details><summary>Evidence for http_expected_response</summary>

```json
{
  "fixture": "http_expected_response"
}
```

</details>

### full

- Status: `EXECUTION_ERROR`
- Reproduction status: `CONFIRMED`
- Mode: `SNAPSHOT`
- Captured: `2026-11-15T20:00:00Z`
- Reason: kubectl rollout status timed out after 120s

| Check | Fault | Status | Reason | Source |
|---|---|---|---|---|

## Provenance

- Contract commit: `4edcd9dfbc48cbd9ca8ea031e429832cf72842d0`
- Fork commit: `12c67cffd45c6b0b8c5da1d2e160a945e2936f49`

| Check | Source |
|---|---|
| `no_oomkilled_in_window` | `scenarios/12-two-faults-oom-and-selector/check.sh#L1` |
| `restart_count_not_increasing` | `scenarios/12-two-faults-oom-and-selector/README.md#L202` |
| `processing_evidence` | `app/shop.yaml#L73` |
| `service_has_endpoints` | `scenarios/12-two-faults-oom-and-selector/check.sh#L2` |
| `endpoints_ready` | `scenarios/12-two-faults-oom-and-selector/check.sh#L2` |
| `http_expected_response` | `app/shop.yaml#L256-L257` |

## Limitations

- This report checks the conditions declared in the incident contract. It does not prove that no other defect exists.
- A SNAPSHOT run is a recording. It does not call Kubernetes and does not prove the live cluster is healthy.
- The verdict is advisory and never blocks a merge.
- Only the faults declared in the contract are covered.
