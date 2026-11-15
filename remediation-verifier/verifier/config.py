"""Paths, defaults and the single lab pin the verifier works against.

The contract and its schema are ordinary files, so the same verifier can run
against another scenario by pointing it at another contract. The environment
variable `REMEDIATION_VERIFIER_CONTRACT` overrides the default path without
touching code, and the schema is resolved next to whichever contract is
loaded, so a pair of files is enough to add a scenario.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = PROJECT_ROOT / "contracts"
DEFAULT_CONTRACT_PATH = CONTRACTS_DIR / "scenario_12.yaml"
SCHEMA_NAME = "schema.json"
ENCODING = "utf-8"

CONTRACT_PATH_ENV = "REMEDIATION_VERIFIER_CONTRACT"

# The lab commit every observation in this project is pinned to. The contract
# carries the same value as data; a test keeps the two in step (D20).
LAB_COMMIT = "4edcd9dfbc48cbd9ca8ea031e429832cf72842d0"


def default_contract_path() -> Path:
    """Return the contract path, honouring the environment override."""
    override = os.environ.get(CONTRACT_PATH_ENV)
    return Path(override).expanduser() if override else DEFAULT_CONTRACT_PATH


def schema_path_for(contract_path: Path) -> Path:
    """Return the schema that belongs to a contract: its sibling file."""
    return contract_path.parent / SCHEMA_NAME
