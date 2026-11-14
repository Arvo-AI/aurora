# Preview stack

A preview is a throwaway copy of the whole Aurora stack, built from the
production images. CI boots one for every pull request
(`.github/workflows/pr-verify.yml`); these scripts are the same steps, so
anything CI does can be reproduced on a laptop.

| Script | What it does |
|---|---|
| `up.sh` | Generates secrets, initialises Vault, starts every service, waits until the API and frontend answer |
| `smoke.sh` | Deterministic checks: containers, health, ports, trust boundaries, a Vault round trip, a log scan |
| `evidence.sh` | Saves container state, resource use and redacted logs to `preview-evidence/` |
| `down.sh` | Removes the containers, the volumes and the preview env file |

## Run it locally

```bash
make down                    # container names and ports are shared with the dev stack
scripts/preview/up.sh        # first run builds the images, which is slow
scripts/preview/smoke.sh

cd client
PLAYWRIGHT_BASE_URL=http://localhost:3000 \
  npx playwright test -c tests/playwright.config.ts --project=chromium
cd ..

scripts/preview/evidence.sh
scripts/preview/down.sh
```

## How it stays out of the way

- **Own Compose project** (`aurora-preview`), so its Postgres and Vault volumes
  are separate from the dev stack's.
- **Own env file** (`.env.preview`, git-ignored). Your `.env` is never read or
  written.
- **No real credentials.** It runs with no LLM key and no cloud accounts, so
  anything that needs one is out of scope for these checks.

## Why Vault is started first

The application reads `VAULT_TOKEN` from its environment, but the token does
not exist until Vault has been initialised. `up.sh` starts Vault alone, lets
`vault-init` initialise and unseal it, copies the root token into
`.env.preview`, and only then starts the rest. By hand this is the "check the
vault-init logs and paste the token into `.env`" step of a first install.

## Reading a result

`smoke.sh` prints one line per check and writes `preview-evidence/summary.md`.

- `FAIL` fails the run.
- `WARN` does not. It marks a check whose baseline is not clean yet, such as
  Python tracebacks during boot. Once a warning reads clean on `main`, change
  it to `FAIL` in `smoke.sh` so it cannot regress.
