"""Database schema for SaaS billing tables.
Called from db_utils.py ensure_tables_exist() when AURORA_SAAS_MODE=true.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

BILLING_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS org_subscriptions (
    id VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    org_id VARCHAR(255) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    stripe_customer_id TEXT NOT NULL DEFAULT '',
    stripe_subscription_id TEXT UNIQUE,
    plan_tier TEXT NOT NULL DEFAULT 'free',
    status TEXT NOT NULL DEFAULT 'active',
    billing_period_start TIMESTAMPTZ,
    billing_period_end TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org_id)
);

CREATE INDEX IF NOT EXISTS idx_org_subscriptions_stripe_customer
    ON org_subscriptions(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_org_subscriptions_status
    ON org_subscriptions(status);

CREATE TABLE IF NOT EXISTS org_usage (
    id VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    org_id VARCHAR(255) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    metric_name TEXT NOT NULL,
    usage_count INTEGER NOT NULL DEFAULT 0,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org_id, metric_name, period_start)
);

CREATE INDEX IF NOT EXISTS idx_org_usage_period
    ON org_usage(org_id, period_start);

CREATE TABLE IF NOT EXISTS stripe_events (
    id VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    stripe_event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    org_id VARCHAR(255) REFERENCES organizations(id) ON DELETE SET NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    processed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_stripe_events_type
    ON stripe_events(event_type);
CREATE INDEX IF NOT EXISTS idx_stripe_events_status
    ON stripe_events(status) WHERE status = 'failed';

-- RLS policies for billing tables
ALTER TABLE org_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_usage ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'org_subscriptions' AND policyname = 'org_subscriptions_org_isolation'
    ) THEN
        CREATE POLICY org_subscriptions_org_isolation ON org_subscriptions
            USING (org_id = current_setting('myapp.current_org_id', true));
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'org_usage' AND policyname = 'org_usage_org_isolation'
    ) THEN
        CREATE POLICY org_usage_org_isolation ON org_usage
            USING (org_id = current_setting('myapp.current_org_id', true));
    END IF;
END $$;
"""


def ensure_billing_tables(cursor, conn) -> None:
    """Create billing tables if they don't exist. Only called in SaaS mode."""
    try:
        cursor.execute(BILLING_TABLES_SQL)
        conn.commit()
        logger.info("[BILLING] Billing tables ensured")
    except Exception as e:
        conn.rollback()
        logger.error("[BILLING] Failed to create billing tables: %s", e)
        raise
