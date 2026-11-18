# Incident-guided browser testing

This package turns an incident brief into a small frozen browser scenario and
replays that scenario against a running preview. The model chooses which visible
rows contain the evidence; the runner, not the model, compares the browser's
badge readings with the frozen expectations.

## Toronto Road Board demo configuration

Configure the connected repository once in Aurora with:

- install command: `python -m pip install -r requirements.txt`
- start command: `python -m flask --app app run --host 0.0.0.0 --port 5000`
- preview port: `5000`
- readiness path: `/`
- readiness timeout: `30` seconds
- scenario: `browser_testing/fixtures/avenue-rd-scenario.json`
- model: `gpt-5-mini`

The frozen Avenue Road scenario hash is
`62b309720e26717a46a6195f47ba7d207bf356ee51dfa022bd9ca87926da3e90`.

Install the isolated CI dependencies and Chromium:

```console
python -m pip install --require-hashes --only-binary=:all: -r browser_testing/requirements-ci.txt
python -m playwright install chromium
```

Author and freeze the Avenue Road scenario while the baseline app is running:

```console
python -m browser_testing author \
  --brief avenue-rd-incident.md \
  --preview-url http://127.0.0.1:5000 \
  --incident-id 40fc996b-ccde-4553-a3e3-ea2046b80bb4 \
  --incident-reference http://localhost:3000/incidents/40fc996b-ccde-4553-a3e3-ea2046b80bb4 \
  --baseline-commit 0e254410ea011f64764c3b362ec213c905b52614 \
  --model gpt-5-mini
```

Replay the checked-in scenario against a candidate commit:

```console
python -m browser_testing run \
  --scenario avenue-rd-scenario.json \
  --preview-url http://127.0.0.1:5000 \
  --commit "$TESTED_COMMIT" \
  --model gpt-5-mini
```

Scenario and brief paths are restricted to `browser_testing/fixtures`. The
replay writes fixed `browser-result.json` and `browser-evidence.png` files only
under `browser_testing/artifacts`; paths outside those directories are rejected.
Exit code zero means all complete readings matched. Exit code one means complete browser
evidence contained a mismatch. Exit code two means the run was inconclusive,
for example because a row was missing or ambiguous or the browser/model failed.
Every run uses a fresh Playwright Chromium context, stays on the preview origin,
and closes the context when it ends.
