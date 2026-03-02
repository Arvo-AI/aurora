"""Casbin RBAC enforcer singleton.

Initialises a Casbin enforcer backed by the Aurora PostgreSQL database via
the SQLAlchemy adapter.  The ``casbin_rule`` table is created automatically
on first connection.
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
# Format: (role, resource, action)
_DEFAULT_POLICIES = [
    # --- viewer permissions (read-only) ---
    ("viewer", "incidents", "read"),
    ("viewer", "postmortems", "read"),
    ("viewer", "dashboards", "read"),
    ("viewer", "connectors", "read"),
    ("viewer", "chat", "read"),
    ("viewer", "chat", "write"),
    ("viewer", "knowledge_base", "read"),
    ("viewer", "ssh_keys", "read"),
    ("viewer", "vms", "read"),
    ("viewer", "llm_usage", "read"),
    ("viewer", "graph", "read"),
    ("viewer", "user_preferences", "read"),
    ("viewer", "user_preferences", "write"),
    ("viewer", "rca_emails", "read"),

    # --- editor permissions (mutating operations) ---
    ("editor", "connectors", "write"),
    ("editor", "incidents", "write"),
    ("editor", "postmortems", "write"),
    ("editor", "knowledge_base", "write"),
    ("editor", "ssh_keys", "write"),
    ("editor", "vms", "write"),
    ("editor", "rca_emails", "write"),
    ("editor", "graph", "write"),

    # --- admin-only permissions ---
    ("admin", "users", "manage"),
    ("admin", "llm_config", "write"),
    ("admin", "llm_config", "read"),
    ("admin", "admin", "access"),
]

# Role hierarchy: admin > editor > viewer
_DEFAULT_ROLE_HIERARCHY = [
    ("admin", "editor"),
    ("editor", "viewer"),
]


def _build_db_url() -> str:
    """Build a SQLAlchemy-compatible database URL from environment variables."""
    db_name = os.environ["POSTGRES_DB"]
    db_user = os.environ["POSTGRES_USER"]
    db_password = os.getenv("POSTGRES_PASSWORD", "")
    db_host = os.environ["POSTGRES_HOST"]
    db_port = os.environ["POSTGRES_PORT"]
    return f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"


def _model_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "..", "rbac_model.conf")


def _seed_default_policies(enforcer: casbin.Enforcer) -> None:
    """Seed default permission and role-hierarchy policies when the table is empty."""
    existing = enforcer.get_policy()
    if existing:
        logger.info("Casbin policies already present (%d rules), skipping seed.", len(existing))
        return

    logger.info("Seeding default Casbin RBAC policies …")

    for role, resource, action in _DEFAULT_POLICIES:
        enforcer.add_policy(role, resource, action)

    for parent_role, child_role in _DEFAULT_ROLE_HIERARCHY:
        enforcer.add_grouping_policy(parent_role, child_role)

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
