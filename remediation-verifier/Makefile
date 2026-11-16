# Remediation verifier: local workflows.
#
# Every target works with or without an activated virtualenv: if `.venv`
# exists it is used, otherwise the interpreter from PATH. That is what lets
# the same targets run locally and in CI without a second code path.
#
# Run make from this directory. The paths below are relative to it, which is
# what `make -C remediation-verifier` hands the CI workflow.

VENV ?= .venv
PYTHON ?= python3

# LIVE reads a checkout of aurora-lab. It is cloned into a cache directory, so
# the workspace checkout is never modified and the clone keeps its LF scripts
# whatever the host git believes about line endings.
LAB ?= .cache/aurora-lab
LAB_REPO ?= https://github.com/Arvo-AI/aurora-lab.git

# Empty by default: the report carries the moment it ran. Set it to reproduce
# a byte-identical bundle, for example GENERATED_AT=2026-11-16T03:00:00Z.
GENERATED_AT ?=
GENERATED_FLAG = $(if $(GENERATED_AT),--generated-at $(GENERATED_AT),)

ifeq ($(OS),Windows_NT)
VENV_PYTHON := $(VENV)/Scripts/python.exe
else
VENV_PYTHON := $(VENV)/bin/python
endif

ifeq ($(wildcard $(VENV_PYTHON)),)
RUN := $(PYTHON)
else
RUN := $(VENV_PYTHON)
endif

.DEFAULT_GOAL := help
.PHONY: help install doctor lab-pin lab-up format lint lint-ci typecheck test selfcheck verify-snapshot verify capture ci clean

help: ## Show the available targets.
	@echo "remediation-verifier targets:"
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

install: ## Create the virtualenv and install runtime and dev dependencies.
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -e ".[dev]"

doctor: ## Report whether this machine can run the verifier.
	$(RUN) -m verifier.doctor

lab-pin: ## Clone aurora-lab into the cache directory at the pinned commit.
	@set -eu; \
	commit=$$($(RUN) -c 'from verifier.config import LAB_COMMIT; print(LAB_COMMIT)'); \
	if [ ! -d "$(LAB)/.git" ]; then \
		mkdir -p "$(dir $(LAB))"; \
		git clone --quiet "$(LAB_REPO)" "$(LAB)"; \
	fi; \
	head=$$(git -C "$(LAB)" rev-parse HEAD 2>/dev/null || true); \
	if [ "$$head" != "$$commit" ]; then \
		git -C "$(LAB)" -c core.autocrlf=false -c core.eol=lf \
			checkout --quiet --force "$$commit"; \
		head=$$(git -C "$(LAB)" rev-parse HEAD); \
	fi; \
	printf 'lab: %s at %s\n' "$$head" "$(LAB)"

lab-up: lab-pin ## Create the kind cluster if it is missing (idempotent).
	@bash "$(LAB)/lab.sh" up

format: ## Apply the formatter and the safe lint fixes.
	$(RUN) -m ruff check --fix .
	$(RUN) -m ruff format .

lint: ## Fail on lint or formatting problems.
	$(RUN) -m ruff check .
	$(RUN) -m ruff format --check .

lint-ci: ## Lint the GitHub Actions workflow (local only; needs actionlint).
	@command -v actionlint >/dev/null 2>&1 \
		|| { echo "actionlint not installed; see https://github.com/rhysd/actionlint"; exit 1; }
	actionlint ci/github-actions-verify.yml

typecheck: ## Type-check the verifier package.
	$(RUN) -m mypy verifier

test: ## Run the test suite.
	$(RUN) -m pytest

selfcheck: ## Clone this branch into a temp directory and run every gate there.
	@set -eu; \
	work=$$(mktemp -d); \
	trap 'rm -rf "$$work"' EXIT; \
	branch=$$(git rev-parse --abbrev-ref HEAD); \
	root=$$(git rev-parse --show-toplevel); \
	echo "selfcheck: cloning $$branch from $$root into $$work"; \
	git clone --quiet --branch "$$branch" "$$root" "$$work/repo"; \
	cd "$$work/repo/remediation-verifier"; \
	$(PYTHON) -m venv .venv; \
	./.venv/bin/python -m pip -q install --upgrade pip; \
	./.venv/bin/python -m pip -q install -e ".[dev]"; \
	make ci

verify-snapshot: ## Decide the committed fixtures and print the table, with no cluster.
	$(RUN) -m verifier --mode snapshot --out out $(GENERATED_FLAG)

verify: lab-pin lab-up ## Run every variant against the lab and print LIVE evidence.
	$(RUN) -m verifier --mode live --lab "$(LAB)" --out out $(GENERATED_FLAG)

capture: lab-pin lab-up ## Run every variant LIVE and rewrite fixtures/snapshots.
	@echo "capture: the lab must sit at the pinned commit; this rewrites fixtures/snapshots"
	$(RUN) -m verifier --mode capture --lab "$(LAB)" \
		--fixtures fixtures/snapshots --out out $(GENERATED_FLAG)

ci: lint typecheck test verify-snapshot ## Run every gate that needs no cluster.

clean: ## Remove generated artifacts and caches, keeping the lab checkout.
	rm -rf out .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
