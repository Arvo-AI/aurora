# Tested alert proposals

Aurora's Alert Gap Audit already proposes monitoring rules. This change adds
`verify_prometheus_alert`: a native agent tool that runs **real Prometheus
`promtool test rules`** before describing a proposed Prometheus alert as tested.
The result includes the actual output, elapsed time, evidence label, SHA-256
hashes, and portable `rules.yml` / `tests.yml` exports.

## Why this change

As agents write more operational configuration, reviewers will need executable
evidence alongside the proposed changes. Aurora can turn an incident's expected
detection behavior into tests that travel with an alert proposal and catch later
regressions. This is one bounded step toward that production-to-code loop.

The demo holds one suite fixed while comparing three candidates:

| Candidate | Duration | Expected result |
| --- | --- | --- |
| `rules-missed.yml` | 10 minutes | Fails: misses the sustained failure at minute 3 |
| `rules-noisy.yml` | 0 minutes | Fails: pages on the brief spike at minute 1 |
| `rules-corrected.yml` | 2 minutes | Passes: detects the failure, ignores the controls, clears on recovery |

All samples are **synthetic**. `checkout_error_ratio` is an explicit input gauge,
not a live query. Expected firing times are fixed independently of the candidate
rules. A passing fixture establishes only its specified behavior; it does not
prove production coverage or a production false-positive rate.

## Run the real demo

Requirements: Python 3.12 and `promtool` 3.5.0 on PATH. Use the official
[Prometheus release](https://github.com/prometheus/prometheus/releases/tag/v3.5.0)
for your platform and verify the archive against its `sha256sums.txt` before
extracting `promtool`. The Aurora server Docker image includes the Linux binary
from a digest-pinned, multi-platform Prometheus image.

From the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --only-binary :all: pytest==9.0.3 PyYAML==6.0.1 pydantic==2.13.3 langchain-core==1.6.13
promtool --version
.venv/bin/python scripts/demo_tested_alerts.py --output-dir /tmp/aurora-alert-demo
.venv/bin/python -m pytest server/tests/services/test_alert_validation.py server/tests/chat/test_prometheus_alert_tool.py -v
```

The demo invokes the **same `build_prometheus_alert_tool` factory used by the
production registry**, including the real LangChain tool schema, Python service,
and promtool process. It requires no LLM API key, account, database, or monitoring
connection. It exits nonzero if a result differs from the expected matrix, if
the intended failure is not present, or if the suite hash changes.

Each exported directory contains `rules.yml`, `tests.yml`, and `receipt.json`.
Independently reproduce the corrected result without Aurora:

```sh
cd /tmp/aurora-alert-demo/corrected
promtool test rules tests.yml
```

Running that command in `missed` or `noisy` must fail. The dedicated
`Prometheus Alert Validation` GitHub Actions workflow installs the same pinned
Linux evaluator and runs both the tests and demo; real-engine tests skip when
promtool is absent from a general developer test environment, but the dedicated
workflow and demo require it.

## Aurora integration

- `server/chat/backend/agent/tools/cloud_tools.py` registers the tool through the
  normal context, notification, and capture wrappers. It does not need a
  monitoring connector and is available in Ask mode because it only evaluates
  supplied data in temporary files.
- `server/chat/backend/agent/tools/prometheus_alert_tool.py` owns its schema and
  shared registration factory.
- `server/services/alert_validation.py` runs the evaluator. No LLM decides the
  pass/fail result.
- `server/services/actions/alert_gap_action.py` adds verification to the existing
  Alert Gap Audit instructions, keeps expectations fixed while tuning, and asks
  for the exact exported rule/tests and receipt information in the proposal.
- `server/Dockerfile` packages promtool 3.5.0 and its license files for the API,
  chatbot, and worker images. No runtime Docker socket is needed.

To try this in a complete local Aurora deployment, use the normal `make init`
setup, configure your model, then **`make prod-local`** to build this branch.
`make prod-prebuilt` pulls released images and will not contain these changes.
In chat, supply the example rule and test YAML and ask Aurora to call
`verify_prometheus_alert` with `evidence_kind="synthetic"`.

For the action workflow, connect a repository containing native Prometheus rules
and enable/run Alert Gap Audit. New organizations receive the updated default
instructions. **Existing organizations must edit that action's saved instructions
to match the updated `DEFAULT_ALERT_GAP_INSTRUCTIONS`.** Existing rows retain
their saved instructions; their Restore default button also uses the previously
stored default. This change deliberately does not migrate customer instructions.

The verification step is an agent instruction, **not a universal enforcement
gate on all PR creation tools**. The executable result is deterministic; an
autonomous agent following the complete action remains a separate integration
boundary. The headless demo does not claim to run the Action/DB/UI lifecycle or
an LLM-generated proposal.

## Scope and failure behavior

One group and one native Prometheus alert; no recording-rule dependencies,
Grafana-managed alerts, provider resources, or unrendered Helm templates.
Standard test YAML supports explicit finite numeric samples and
`alert_rule_test` expectations. At least one firing and one nonfiring assertion
are required. Assertions must fit within every supplied series' sample window.

Limits: 8 KB per input and canonical export, eight test cases, 16 series per case,
4,096 total samples, 32 assertions, and a one-hour horizon. YAML aliases,
duplicate keys, external paths/globs, unknown fields, expanding sample notation,
and nonfinite samples are rejected. There are no caller-provided shell commands
or executable paths. The process receives a minimal environment, a 10-second
timeout, and capped output; temporary files are removed after execution.

`passed` means promtool returned zero. `failed` means the evaluator rejected the
rule/tests (inspect the actual output). Missing executors, timeouts, crashes, and
input-validation failures return `error`, never a successful verification.
`recorded` is a caller-declared provenance label, not authenticated telemetry.
Input hashes let reviewers identify the exact tested exports; they do not
cryptographically attest a model's subsequent PR contents.

## Next step

Accept explicitly sourced recorded metric windows, retaining timestamps,
lookback, and provenance. Then bind the tested rule/test hashes to the exact PR
files at submission time, so editing a proposal after validation invalidates its
verification receipt. Neither automatic historical retrieval nor that PR gate is
claimed in this submission.
