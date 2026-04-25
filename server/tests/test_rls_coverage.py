"""Verify every table with an org_id column is either RLS-protected or explicitly excluded.

Parses db_utils.py statically to find CREATE TABLE statements with org_id
columns and compares them against the rls_tables list. If a new table with
org_id is added without being in either list, this test fails.
"""

import re
from pathlib import Path
from typing import Set

_DB_UTILS = Path(__file__).resolve().parent.parent / "utils" / "db" / "db_utils.py"

# Tables with org_id that are intentionally excluded from RLS.
# Each entry MUST have a comment explaining why.
RLS_EXCLUSIONS: Set[str] = {
    "users",                      # queried during login before org context is set
    "audit_log",                  # written with explicit org_id outside RLS session scope
    "org_invitations",            # queried during invite/join flows before org context
}


def _parse_tables_with_orgid(source: str) -> Set[str]:
    tables: Set[str] = set()
    for m in re.finditer(
        r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\);", source, re.DOTALL
    ):
        if "org_id" in m.group(2):
            tables.add(m.group(1))
    return tables


def _parse_rls_tables(source: str) -> Set[str]:
    block = source[source.index("rls_tables = ["):]
    block = block[:block.index("# Commit table creation and RLS")]
    tables: Set[str] = set()
    for m in re.finditer(r'rls_tables\s*=\s*\[(.*?)\]', block, re.DOTALL):
        for name in re.findall(r'"(\w+)"', m.group(1)):
            tables.add(name)
    for m in re.finditer(r'rls_tables\.append\("(\w+)"\)', block):
        tables.add(m.group(1))
    return tables


def test_all_orgid_tables_covered():
    """Every table with org_id must be in rls_tables or RLS_EXCLUSIONS."""
    source = _DB_UTILS.read_text()
    orgid_tables = _parse_tables_with_orgid(source)
    rls_tables = _parse_rls_tables(source)

    covered = rls_tables | RLS_EXCLUSIONS
    uncovered = orgid_tables - covered

    assert not uncovered, (
        f"Tables with org_id missing from both rls_tables and RLS_EXCLUSIONS: "
        f"{sorted(uncovered)}. Add them to rls_tables in db_utils.py or to "
        f"RLS_EXCLUSIONS in this test with a comment explaining why."
    )


def test_no_rls_on_tables_without_orgid():
    """Tables in rls_tables must actually have an org_id column."""
    source = _DB_UTILS.read_text()
    orgid_tables = _parse_tables_with_orgid(source)
    rls_tables = _parse_rls_tables(source)

    bogus = rls_tables - orgid_tables
    assert not bogus, (
        f"Tables in rls_tables that have no org_id column: {sorted(bogus)}. "
        f"Remove them from rls_tables — RLS policies referencing org_id will fail."
    )


def test_exclusions_still_needed():
    """RLS_EXCLUSIONS entries must still exist as tables with org_id."""
    source = _DB_UTILS.read_text()
    orgid_tables = _parse_tables_with_orgid(source)

    stale = RLS_EXCLUSIONS - orgid_tables
    assert not stale, (
        f"RLS_EXCLUSIONS entries that no longer exist as tables with org_id: "
        f"{sorted(stale)}. Remove stale entries from RLS_EXCLUSIONS."
    )
