# Remediation verifier — local workflows.
#
# Every target works with or without an activated virtualenv: if `.venv`
# exists it is used, otherwise the interpreter from PATH. That is what lets
# the same targets run locally and in CI without a second code path.

VENV ?= .venv
PYTHON ?= python3

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
.PHONY: help install doctor format lint lint-ci typecheck test selfcheck ci clean

help: ## Show the available targets.
	@echo "remediation-verifier targets:"
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

install: ## Create the virtualenv and install runtime and dev dependencies.
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -e ".[dev]"

doctor: ## Report whether this machine can run the verifier.
	$(RUN) -m verifier.doctor

format: ## Apply the formatter and the safe lint fixes.
	$(RUN) -m ruff check --fix .
	$(RUN) -m ruff format .

lint: ## Fail on lint or formatting problems.
	$(RUN) -m ruff check .
	$(RUN) -m ruff format --check .

lint-ci: ## Lint the GitHub Actions workflow (local only; needs actionlint).
	@command -v actionlint >/dev/null 2>&1 \
		|| { echo "actionlint not installed; see docs/12-skills-and-toolchain.md"; exit 1; }
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

ci: lint typecheck test ## Run every gate that needs no cluster.

clean: ## Remove generated artifacts and caches.
	rm -rf out .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
