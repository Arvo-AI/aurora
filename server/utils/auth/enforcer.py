"""Casbin RBAC enforcer singleton with domain (org) support.

Initialises a Casbin enforcer backed by the Aurora PostgreSQL database via
the SQLAlchemy adapter.  The ``casbin_rule`` table is created automatically
on first connection.

Domain-based RBAC: every policy and role assignment is scoped to an org_id
(the "domain" in Casbin terminology).  Wildcard domain "*" policies apply
to all orgs.
"""

import logging
import os
import threading

import casbin
from casbin_sqlalchemy_adapter import Adapter

logger = logging.getLogger(__name__)

_enforcer: casbin.Enforcer | None = None
_lock = threading.Lock()

# Default permission policies seeded on first run.
# Format: (role, domain, resource, action)
# domain="*" means the policy applies to every org.
_DEFAULT_POLICIES = [
    # --- viewer permissions (read-only) ---
    ("viewer", "*", "incidents", "read"),
    ("viewer", "*", "postmortems", "read"),
    ("viewer", "*", "dashboards", "read"),
    ("viewer", "*", "connectors", "read"),
    ("viewer", "*", "chat", "read"),
    ("viewer", "*", "chat", "write"),
    ("viewer", "*", "knowledge_base", "read"),
    ("viewer", "*", "ssh_keys", "read"),
    ("viewer", "*", "vms", "read"),
    ("viewer", "*", "llm_usage", "read"),
    ("viewer", "*", "graph", "read"),
    ("viewer", "*", "user_preferences", "read"),
    ("viewer", "*", "user_preferences", "write"),
    ("viewer", "*", "rca_emails", "read"),

    # --- editor permissions (mutating operations) ---
    ("editor", "*", "connectors", "write"),
    ("editor", "*", "incidents", "write"),
    ("editor", "*", "postmortems", "write"),
    ("editor", "*", "knowledge_base", "write"),
    ("editor", "*", "ssh_keys", "write"),
    ("editor", "*", "vms", "write"),
    ("editor", "*", "rca_emails", "write"),
    ("editor", "*", "graph", "write"),

    # --- admin-only permissions ---
    ("admin", "*", "users", "manage"),
    ("admin", "*", "llm_config", "write"),
    ("admin", "*", "llm_config", "read"),
    ("admin", "*", "admin", "access"),
    ("admin", "*", "org", "manage"),
]

# Role hierarchy: admin > editor > viewer
# With domains, grouping is (parent_role, child_role, domain).
# Using "*" so the hierarchy applies in all orgs.
_DEFAULT_ROLE_HIERARCHY = [
    ("admin", "editor", "*"),
    ("editor", "viewer", "*"),
]


def _build_db_url() -> str:
    """Build a SQLAlchemy-compatible database URL from environment variables."""
    import urllib.parse
    db_name = os.environ["POSTGRES_DB"]
    db_user = os.environ["POSTGRES_USER"]
    db_password = urllib.parse.quote_plus(os.getenv("POSTGRES_PASSWORD", ""))
    db_host = os.environ["POSTGRES_HOST"]
    db_port = os.environ["POSTGRES_PORT"]
    return f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"


def _model_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "..", "rbac_model.conf")


def _seed_default_policies(enforcer: casbin.Enforcer) -> None:
    """Seed default permission and role-hierarchy policies when the table is empty.
    
    Also handles migration from non-domain to domain-based model by checking
    if existing policies have the old 3-field format and re-seeding.
    """
    existing = enforcer.get_policy()
    if existing:
        # Check if policies are old format (3 fields) vs new (4 fields with domain)
        needs_migration = any(len(p) == 3 for p in existing)
        if needs_migration:
            logger.info("Detected old non-domain Casbin policies, migrating to domain-based format...")
            enforcer.clear_policy()
        else:
            logger.info("Casbin policies already present (%d rules), skipping seed.", len(existing))
            return

    logger.info("Seeding default Casbin RBAC policies …")

    for role, domain, resource, action in _DEFAULT_POLICIES:
        enforcer.add_policy(role, domain, resource, action)

    for parent_role, child_role, domain in _DEFAULT_ROLE_HIERARCHY:
        enforcer.add_grouping_policy(parent_role, child_role, domain)

    enforcer.save_policy()
    logger.info("Default Casbin policies seeded successfully.")


def get_enforcer() -> casbin.Enforcer:
    """Return the module-level Casbin enforcer, creating it on first call."""
    global _enforcer
    if _enforcer is not None:
        return _enforcer

    with _lock:
        if _enforcer is not None:
            return _enforcer

        db_url = _build_db_url()
        model_path = _model_path()
        logger.info("Initialising Casbin enforcer (model=%s)", model_path)

        adapter = Adapter(db_url)
        _enforcer = casbin.Enforcer(model_path, adapter)

        def _domain_match(key1: str, key2: str) -> bool:
            """Match org (domain) in Casbin grouping policies.

            Supports exact match and wildcard ``*`` (used for policies that
            apply across all organisations, e.g. the built-in role definitions).
            """
            return key1 == key2 or key2 == "*"

        _enforcer.add_named_domain_matching_func("g", _domain_match)

        _seed_default_policies(_enforcer)
        _enforcer.load_policy()

        logger.info("Casbin enforcer ready.")
        return _enforcer


def reload_policies() -> None:
    """Reload all policies from the database into memory.

    Call this after any admin mutation (role assign / revoke) so that the
    in-process enforcer cache stays current.
    """
    enforcer = get_enforcer()
    enforcer.load_policy()
    logger.info("Casbin policies reloaded from database.")


def assign_role_to_user(user_id: str, role: str, org_id: str) -> None:
    """Assign a role to a user within an org (domain)."""
    enforcer = get_enforcer()
    enforcer.add_grouping_policy(user_id, role, org_id)
    enforcer.save_policy()
    enforcer.load_policy()
    logger.info("Assigned role %s to user %s in org %s", role, user_id, org_id)


def remove_role_from_user(user_id: str, role: str, org_id: str) -> None:
    """Remove a role from a user within an org (domain)."""
    enforcer = get_enforcer()
    enforcer.remove_grouping_policy(user_id, role, org_id)
    enforcer.save_policy()
    enforcer.load_policy()
    logger.info("Removed role %s from user %s in org %s", role, user_id, org_id)


def get_user_roles_in_org(user_id: str, org_id: str) -> list[str]:
    """Get all roles assigned to a user in a specific org."""
    enforcer = get_enforcer()
    return enforcer.get_roles_for_user_in_domain(user_id, org_id)
