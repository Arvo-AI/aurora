import psycopg2
import psycopg2.extras
import logging
from psycopg2 import DatabaseError
from dotenv import load_dotenv
import os
from utils.db.connection_pool import db_pool

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,  # Detailed logs
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# Unified database configuration using POSTGRES_* env vars
# All values must be set via environment (see .env.example)
DB_PARAMS = {
    "dbname": os.environ["POSTGRES_DB"],
    "user": os.environ["POSTGRES_USER"],
    "password": os.getenv("POSTGRES_PASSWORD", ""),
    "host": os.environ["POSTGRES_HOST"],
    "port": int(os.environ["POSTGRES_PORT"]),
}
_pg_sslmode = os.getenv("POSTGRES_SSLMODE", "prefer")
if _pg_sslmode:
    DB_PARAMS["sslmode"] = _pg_sslmode
    _pg_sslrootcert = os.getenv("POSTGRES_SSLROOTCERT")
    if _pg_sslrootcert:
        DB_PARAMS["sslrootcert"] = _pg_sslrootcert


def ensure_database_exists():
    """Ensure that the target database exists.
    On local Docker, connects to 'postgres' DB to check/create the target DB.
    On managed databases (e.g. RDS) where the user may lack access to 'postgres',
    falls back to verifying the target DB is reachable directly."""
    logging.debug("Starting the database existence check.")
    conn = None
    cursor = None
    try:
        # Try connecting to 'postgres' database to check/create target database
        init_params = DB_PARAMS.copy()
        init_params["dbname"] = "postgres"

        logging.debug(f"Connecting to postgres database as {init_params['user']}")
        conn = psycopg2.connect(**init_params)
        conn.autocommit = True
        cursor = conn.cursor()  # No RLS needed — infrastructure DDL/bootstrap
        logging.info("Connected to postgres database.")

        # Create the target database if it doesn't exist
        target_db = DB_PARAMS["dbname"]
        cursor.execute(f"SELECT 1 FROM pg_database WHERE datname = '{target_db}';")
        if not cursor.fetchone():
            cursor.execute(f"CREATE DATABASE {target_db};")
            logging.info(f"Database '{target_db}' created successfully.")
        else:
            logging.info(f"Database '{target_db}' already exists.")

    except psycopg2.OperationalError:
        logging.info(
            "Cannot connect to 'postgres' database (likely managed DB). "
            "Verifying target database '%s' is reachable directly.",
            DB_PARAMS["dbname"],
        )
        if cursor:
            cursor.close()
            cursor = None
        if conn:
            conn.close()
            conn = None
        try:
            conn = psycopg2.connect(**DB_PARAMS)
            logging.info(f"Database '{DB_PARAMS['dbname']}' is reachable.")
        except Exception as e:
            logging.error(f"Cannot connect to target database: {e}")
            raise
    except Exception as e:
        logging.error(f"Error ensuring database exists: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
        logging.debug("Connection closed.")


def connect_to_db_as_admin():
    """
    DEPRECATED: Use db_pool.get_admin_connection() context manager instead.
    Connect to the target database using admin credentials.
    """
    from utils.db.db_adapters import connect_to_db_as_admin as adapter_connect_admin

    return adapter_connect_admin()


def connect_to_db_as_user():
    """
    DEPRECATED: Use db_pool.get_user_connection() context manager instead.
    Connect to the target database using appuser credentials.
    """
    from utils.db.db_adapters import connect_to_db_as_user as adapter_connect_user

    return adapter_connect_user()


def initialize_tables():
    """Create tables and apply RLS policies using the admin connection,
    then transfer ownership to appuser."""
    logging.debug("Initializing Kubernetes database tables using admin credentials.")
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()  # No RLS needed — schema migration/DDL

            # Try to acquire advisory lock (non-blocking)
            cursor.execute("SELECT pg_try_advisory_lock(1234567890);")
            lock_acquired = cursor.fetchone()[0]
            
            if not lock_acquired:
                # Lock is held - likely by a dead process. Force release stale locks.
                logging.warning("Advisory lock held by another process, clearing stale locks...")
                cursor.execute("""
                    SELECT pg_terminate_backend(pid) 
                    FROM pg_locks 
                    WHERE locktype = 'advisory' AND objid = 1234567890 AND pid != pg_backend_pid();
                """)
                conn.commit()
                
                # Now acquire the lock properly
                cursor.execute("SELECT pg_advisory_lock(1234567890);")
                lock_acquired = True
                logging.info("Advisory lock acquired after clearing stale locks")

            # Define table creation scripts.
            create_tables = {
                "k8s_pods": """
                    CREATE TABLE IF NOT EXISTS k8s_pods (
                        id SERIAL PRIMARY KEY,
                        namespace VARCHAR(255) NOT NULL,
                        pod_name VARCHAR(255) NOT NULL,
                        status VARCHAR(50) NOT NULL,
                        project_id VARCHAR(255) NOT NULL,
                        cluster_name VARCHAR(255) NOT NULL,
                        user_id VARCHAR(1000),
                        org_id VARCHAR(255),
                        provider VARCHAR(50),
                        UNIQUE (pod_name, namespace, project_id, cluster_name, user_id)
                    );
                """,
                "k8s_nodes": """
                    CREATE TABLE IF NOT EXISTS k8s_nodes (
                        id SERIAL PRIMARY KEY,
                        node_name VARCHAR(255) NOT NULL,
                        status VARCHAR(50),
                        project_id VARCHAR(255) NOT NULL,
                        cluster_name VARCHAR(255) NOT NULL,
                        user_id VARCHAR(1000),
                        org_id VARCHAR(255),
                        provider VARCHAR(50),
                        UNIQUE (node_name, project_id, cluster_name, user_id)
                    );
                """,
                "k8s_node_conditions": """
                    CREATE TABLE IF NOT EXISTS k8s_node_conditions (
                        id SERIAL PRIMARY KEY,
                        node_name VARCHAR(255) NOT NULL,
                        project_id VARCHAR(255) NOT NULL,
                        cluster_name VARCHAR(255) NOT NULL,
                        last_heartbeat_time TIMESTAMP,
                        last_transition_time TIMESTAMP,
                        message TEXT,
                        reason TEXT,
                        status TEXT,
                        type TEXT,
                        user_id VARCHAR(1000),
                        org_id VARCHAR(255),
                        provider VARCHAR(50),
                        FOREIGN KEY (node_name, project_id, cluster_name, user_id) REFERENCES k8s_nodes (node_name, project_id, cluster_name, user_id) ON DELETE CASCADE,
                        UNIQUE (node_name, project_id, cluster_name, type, user_id)
                    );
                """,
                "k8s_services": """
                    CREATE TABLE IF NOT EXISTS k8s_services (
                        id SERIAL PRIMARY KEY,
                        namespace VARCHAR(255) NOT NULL,
                        service_name VARCHAR(255) NOT NULL,
                        type VARCHAR(50) NOT NULL,
                        project_id VARCHAR(255) NOT NULL,
                        cluster_name VARCHAR(255) NOT NULL,
                        user_id VARCHAR(1000),
                        org_id VARCHAR(255),
                        provider VARCHAR(50),
                        UNIQUE (service_name, namespace, project_id, cluster_name, user_id)

                    );
                """,
                "k8s_deployments": """
                    CREATE TABLE IF NOT EXISTS k8s_deployments (
                        id SERIAL PRIMARY KEY,
                        namespace VARCHAR(255) NOT NULL,
                        deployment_name VARCHAR(255) NOT NULL,
                        replicas INT,
                        project_id VARCHAR(255) NOT NULL,
                        cluster_name VARCHAR(255) NOT NULL,
                        user_id VARCHAR(1000),
                        org_id VARCHAR(255),
                        provider VARCHAR(50),
                        UNIQUE (deployment_name, namespace, project_id, cluster_name, user_id)
                    );
                """,
                "k8s_ingresses": """
                    CREATE TABLE IF NOT EXISTS k8s_ingresses (
                        id SERIAL PRIMARY KEY,
                        namespace VARCHAR(255) NOT NULL,
                        ingress_name VARCHAR(255) NOT NULL,
                        project_id VARCHAR(255) NOT NULL,
                        cluster_name VARCHAR(255) NOT NULL,
                        user_id VARCHAR(1000),
                        org_id VARCHAR(255),
                        provider VARCHAR(50),
                        UNIQUE (ingress_name, namespace, project_id, cluster_name, user_id)
                    );
                """,
                "k8s_pod_metrics": """
                    CREATE TABLE IF NOT EXISTS k8s_pod_metrics (
                        id SERIAL PRIMARY KEY,
                        pod_name VARCHAR(255) NOT NULL,
                        namespace VARCHAR(255) NOT NULL,
                        cpu_usage TEXT,
                        memory_usage TEXT,
                        project_id VARCHAR(255) NOT NULL,
                        cluster_name VARCHAR(255) NOT NULL,
                        user_id VARCHAR(1000),
                        org_id VARCHAR(255),
                        provider VARCHAR(50),
                        UNIQUE (pod_name, namespace, project_id, cluster_name, user_id)
                    );
                """,
                "k8s_node_metrics": """
                    CREATE TABLE IF NOT EXISTS k8s_node_metrics (
                        id SERIAL PRIMARY KEY,
                        node_name VARCHAR(255) NOT NULL,
                        cpu_usage TEXT,
                        memory_usage TEXT,
                        project_id VARCHAR(255) NOT NULL,
                        cluster_name VARCHAR(255) NOT NULL,
                        user_id VARCHAR(1000),
                        org_id VARCHAR(255),
                        provider VARCHAR(50),
                        UNIQUE (node_name, project_id, cluster_name, user_id)
                    );
                """,
                "cloud_billing_usage": """
                    CREATE TABLE IF NOT EXISTS cloud_billing_usage (
                        id SERIAL PRIMARY KEY,
                        service VARCHAR(255) NOT NULL,
                        sku VARCHAR(255),
                        category VARCHAR(255),
                        cost NUMERIC NOT NULL,
                        usage NUMERIC,
                        unit VARCHAR(50),
                        usage_date DATE NOT NULL,
                        region VARCHAR(255),
                        project_id VARCHAR(255) NOT NULL,
                        currency VARCHAR(10),
                        dataset_id VARCHAR(255),
                        table_name VARCHAR(255),
                        user_id VARCHAR(1000),
                        org_id VARCHAR(255),
                        provider VARCHAR(50),
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (service, sku, category, usage_date, region, project_id, dataset_id, user_id)
                    );
                """,
                "provider_metrics": """
                    CREATE TABLE IF NOT EXISTS provider_metrics (
                        id SERIAL PRIMARY KEY,
                        metric_name VARCHAR(255) NOT NULL,
                        value FLOAT NOT NULL,
                        timestamp TIMESTAMP NOT NULL,
                        labels JSONB,
                        resource_type VARCHAR(255),
                        resource_labels JSONB,
                        category VARCHAR(50),
                        unit VARCHAR(50),
                        user_id VARCHAR(50),
                        org_id VARCHAR(255),
                        provider VARCHAR(50),
                        UNIQUE (metric_name, timestamp, labels, resource_type, resource_labels, category, unit, user_id)
                    );
                """,
                "user_tokens": """
                    CREATE TABLE IF NOT EXISTS user_tokens (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        token_data JSONB,
                        secret_ref VARCHAR(512),
                        provider VARCHAR(50) NOT NULL,
                        tenant_id VARCHAR(255),
                        client_id VARCHAR(255),
                        client_secret VARCHAR(255),
                        subscription_name VARCHAR(255),
                        subscription_id VARCHAR(255),
                        email VARCHAR(255),
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        session_data JSONB,
                        last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        is_active BOOLEAN DEFAULT true,
                        UNIQUE(user_id, provider)
                    );
                """,
                "github_connected_repos": """
                    CREATE TABLE IF NOT EXISTS github_connected_repos (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        repo_full_name VARCHAR(512) NOT NULL,
                        repo_id INTEGER,
                        default_branch VARCHAR(255),
                        is_private BOOLEAN DEFAULT false,
                        metadata_summary TEXT,
                        metadata_status VARCHAR(20) DEFAULT 'pending',
                        repo_data JSONB,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, repo_full_name)
                    );
                """,
                "user_manual_vms": """
                    CREATE TABLE IF NOT EXISTS user_manual_vms (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        name VARCHAR(255) NOT NULL,
                        ip_address VARCHAR(45) NOT NULL,
                        port INTEGER DEFAULT 22,
                        ssh_jump_command TEXT,
                        ssh_key_id INTEGER REFERENCES user_tokens(id) ON DELETE SET NULL,
                        ssh_username VARCHAR(255),
                        connection_verified BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW(),
                        UNIQUE(user_id, ip_address, port)
                    );
                """,
                "user_connections": """
                    CREATE TABLE IF NOT EXISTS user_connections (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        provider VARCHAR(50) NOT NULL,
                        account_id VARCHAR(255) NOT NULL,
                        role_arn VARCHAR(512),
                        read_only_role_arn VARCHAR(512),
                        connection_method VARCHAR(50),
                        region VARCHAR(50),
                        status VARCHAR(20) DEFAULT 'active', -- active | not_connected | error
                        last_verified_at TIMESTAMP,
                        UNIQUE(user_id, provider, account_id)
                    );
                """,
                "user_preferences": """
                    CREATE TABLE IF NOT EXISTS user_preferences (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        preference_key VARCHAR(255) NOT NULL,
                        preference_value JSONB,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, org_id, preference_key)
                    );
                """,
                "workspaces": """
                    CREATE TABLE IF NOT EXISTS workspaces (
                        id VARCHAR(50) PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        name VARCHAR(255) NOT NULL,
                        aws_external_id VARCHAR(36),                    -- UUID v4 for ExternalId (needed for STS)
                        aws_discovery_artifact_bucket VARCHAR(255),     -- S3 bucket for mirror.json
                        aws_discovery_artifact_key VARCHAR(255),        -- S3 key for mirror.json  
                        aws_discovery_summary JSONB,                    -- {principal_arn, managed_policy_names, counts}
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """,
                "aurora_deployments": """
                    CREATE TABLE IF NOT EXISTS aurora_deployments (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        deployment_name VARCHAR(255) NOT NULL,
                        project_id VARCHAR(255) NOT NULL,
                        deployment_id VARCHAR(255) UNIQUE NOT NULL,
                        region VARCHAR(100) DEFAULT 'us-central1',
                        status VARCHAR(50) DEFAULT 'creating',
                        service_accounts JSONB,
                        billing_account VARCHAR(255),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        metadata JSONB,
                        UNIQUE(project_id, deployment_name),
                        UNIQUE(user_id, deployment_name)
                    );
                """,
                "deployment_tasks": """
                    CREATE TABLE IF NOT EXISTS deployment_tasks (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        task_id VARCHAR(255) NOT NULL,
                        deployment_id VARCHAR(255),
                        status VARCHAR(50),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        task_data JSONB,
                        UNIQUE(user_id, task_id)
                    );
                """,
                "deployments": """
                    CREATE TABLE IF NOT EXISTS deployments (
                        id VARCHAR(50) PRIMARY KEY,
                        name VARCHAR(255) NOT NULL,
                        status VARCHAR(50) NOT NULL,
                        provider VARCHAR(50) NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        project_id VARCHAR(255),
                        account_id VARCHAR(255),
                        cluster_name VARCHAR(255),
                        details JSONB,
                        url VARCHAR(255),
                        type VARCHAR(100),
                        error_msg VARCHAR(1000),
                        user_id VARCHAR(1000),
                        org_id VARCHAR(255),
                        task_id VARCHAR(100),
                        service_name_map JSONB
                    );
                """,
                "chat_sessions": """
                    CREATE TABLE IF NOT EXISTS chat_sessions (
                        id VARCHAR(50) PRIMARY KEY,
                        user_id VARCHAR(1000) NOT NULL,
                        org_id VARCHAR(255),
                        title VARCHAR(255) NOT NULL,
                        messages JSONB DEFAULT '[]'::jsonb,
                        ui_state JSONB DEFAULT '{}'::jsonb,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        is_active BOOLEAN DEFAULT true,
                        status VARCHAR(20) DEFAULT 'active',
                        incident_id UUID
                    );
                """,
                "llm_usage_tracking": """
                    CREATE TABLE IF NOT EXISTS llm_usage_tracking (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(1000) NOT NULL,
                        org_id VARCHAR(255),
                        session_id VARCHAR(50),
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        model_name VARCHAR(255) NOT NULL,
                        api_provider VARCHAR(100) DEFAULT 'openrouter',
                        request_type VARCHAR(100),
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER GENERATED ALWAYS AS (input_tokens + output_tokens) STORED,
                        estimated_cost DECIMAL(10,6) DEFAULT 0.00,
                        surcharge_rate DECIMAL(5,4) DEFAULT 0.0000,
                        surcharge_amount DECIMAL(10,6) GENERATED ALWAYS AS (estimated_cost * surcharge_rate) STORED,
                        total_cost_with_surcharge DECIMAL(10,6) GENERATED ALWAYS AS (estimated_cost * (1 + surcharge_rate)) STORED,
                        response_time_ms INTEGER,
                        error_message TEXT,
                        request_metadata JSONB DEFAULT '{}'::jsonb,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """,
                "cloud_feed_metadata": """
                    CREATE TABLE IF NOT EXISTS cloud_feed_metadata (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        project_id VARCHAR(255) NOT NULL,
                        provider VARCHAR(50) NOT NULL,
                        feed_name VARCHAR(255) NOT NULL,
                        feed_status VARCHAR(50) DEFAULT 'active',
                        topic_name VARCHAR(255) NOT NULL,
                        subscription_name VARCHAR(255) NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_notification_at TIMESTAMP,
                        notification_count INTEGER DEFAULT 0,
                        UNIQUE(user_id, project_id, provider)
                    );
                    
                    CREATE INDEX IF NOT EXISTS idx_feed_status ON cloud_feed_metadata(feed_status) WHERE feed_status = 'active';
                    CREATE INDEX IF NOT EXISTS idx_feed_user_project ON cloud_feed_metadata(user_id, project_id);
                """,
                "cloud_ingestion_state": """
                    CREATE TABLE IF NOT EXISTS cloud_ingestion_state (
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        provider VARCHAR(50) NOT NULL,
                        in_progress BOOLEAN DEFAULT FALSE,
                        total_projects INTEGER,
                        completed_projects INTEGER,
                        started_at TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (user_id, provider)
                    );
                    
                    CREATE INDEX IF NOT EXISTS idx_ingestion_in_progress ON cloud_ingestion_state(user_id, provider) WHERE in_progress = TRUE;
                """,
                "grafana_alerts": """
                    CREATE TABLE IF NOT EXISTS grafana_alerts (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        alert_uid VARCHAR(255),
                        alert_title TEXT,
                        alert_state VARCHAR(50),
                        rule_name TEXT,
                        rule_url TEXT,
                        dashboard_url TEXT,
                        panel_url TEXT,
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    
                    CREATE INDEX IF NOT EXISTS idx_grafana_alerts_user_id ON grafana_alerts(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_grafana_alerts_state ON grafana_alerts(alert_state);
                    CREATE INDEX IF NOT EXISTS idx_grafana_alerts_received_at ON grafana_alerts(received_at DESC);
                """,
                "datadog_events": """
                    CREATE TABLE IF NOT EXISTS datadog_events (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        event_type VARCHAR(100),
                        event_title TEXT,
                        status VARCHAR(50),
                        scope TEXT,
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_datadog_events_user_id ON datadog_events(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_datadog_events_status ON datadog_events(status);
                    CREATE INDEX IF NOT EXISTS idx_datadog_events_received_at ON datadog_events(received_at DESC);
                """,
                "newrelic_events": """
                    CREATE TABLE IF NOT EXISTS newrelic_events (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        issue_id VARCHAR(255),
                        issue_title TEXT,
                        priority VARCHAR(20),
                        state VARCHAR(50),
                        entity_names TEXT,
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_newrelic_events_org_issue
                        ON newrelic_events(org_id, issue_id) WHERE issue_id IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS idx_newrelic_events_user_id ON newrelic_events(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_newrelic_events_state ON newrelic_events(state);
                    CREATE INDEX IF NOT EXISTS idx_newrelic_events_received_at ON newrelic_events(received_at DESC);
                """,
                "netdata_alerts": """
                    CREATE TABLE IF NOT EXISTS netdata_verification_tokens (
                        user_id TEXT PRIMARY KEY,
                        token TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS netdata_alerts (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        alert_name VARCHAR(255),
                        alert_status VARCHAR(50),
                        alert_class VARCHAR(100),
                        alert_family VARCHAR(255),
                        chart VARCHAR(255),
                        host VARCHAR(255),
                        space VARCHAR(255),
                        room VARCHAR(255),
                        value TEXT,
                        message TEXT,
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        alert_hash VARCHAR(64) UNIQUE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_netdata_alerts_user_id ON netdata_alerts(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_netdata_alerts_status ON netdata_alerts(alert_status);
                    CREATE INDEX IF NOT EXISTS idx_netdata_alerts_received_at ON netdata_alerts(received_at DESC);
                """,
                "pagerduty_events": """
                    CREATE TABLE IF NOT EXISTS pagerduty_events (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        event_type VARCHAR(100),
                        incident_id VARCHAR(255),
                        incident_title TEXT,
                        incident_status VARCHAR(50),
                        incident_urgency VARCHAR(20),
                        service_name VARCHAR(255),
                        service_id VARCHAR(255),
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_pagerduty_events_user_id ON pagerduty_events(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_pagerduty_events_incident_id ON pagerduty_events(incident_id);
                    CREATE INDEX IF NOT EXISTS idx_pagerduty_events_status ON pagerduty_events(incident_status);
                    CREATE INDEX IF NOT EXISTS idx_pagerduty_events_received_at ON pagerduty_events(received_at DESC);
                """,
                "opsgenie_events": """
                    CREATE TABLE IF NOT EXISTS opsgenie_events (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        action VARCHAR(100),
                        alert_id VARCHAR(255),
                        alert_message TEXT,
                        priority VARCHAR(10),
                        status VARCHAR(50),
                        source VARCHAR(255),
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_opsgenie_events_user_id ON opsgenie_events(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_opsgenie_events_alert_id ON opsgenie_events(alert_id);
                    CREATE INDEX IF NOT EXISTS idx_opsgenie_events_received_at ON opsgenie_events(received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_opsgenie_events_status ON opsgenie_events(status);
                """,
                "incidents": """
                     CREATE TABLE IF NOT EXISTS incidents (
                         id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                         user_id VARCHAR(255) NOT NULL,
                         org_id VARCHAR(255),
                         source_type VARCHAR(20) NOT NULL,
                         source_alert_id INTEGER NOT NULL,
                         status VARCHAR(20) NOT NULL DEFAULT 'investigating',
                         severity VARCHAR(20),
                         alert_title TEXT,
                         alert_service TEXT,
                         alert_environment TEXT,
                         aurora_status VARCHAR(20) DEFAULT 'idle',
                         aurora_summary TEXT,
                         aurora_chat_session_id UUID,
                         started_at TIMESTAMP NOT NULL,
                         analyzed_at TIMESTAMP,
                         slack_message_ts VARCHAR(50),
                         google_chat_message_name VARCHAR(255),
                         active_tab VARCHAR(10) DEFAULT 'thoughts',
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         merged_into_incident_id UUID REFERENCES incidents(id) ON DELETE SET NULL,
                         UNIQUE(org_id, source_type, source_alert_id, user_id)
                     );
                     
                     CREATE INDEX IF NOT EXISTS idx_incidents_user_id ON incidents(user_id, started_at DESC);
                     CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
                     CREATE INDEX IF NOT EXISTS idx_incidents_source ON incidents(source_type, source_alert_id);
                     CREATE INDEX IF NOT EXISTS idx_incidents_merged ON incidents(merged_into_incident_id) WHERE merged_into_incident_id IS NOT NULL;
                 """,
                "incident_alerts": """
                    CREATE TABLE IF NOT EXISTS incident_alerts (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id VARCHAR(1000) NOT NULL,
                        org_id VARCHAR(255),
                        incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
                        source_type VARCHAR(20) NOT NULL,
                        source_alert_id INTEGER NOT NULL,
                        alert_title TEXT,
                        alert_service TEXT,
                        alert_severity VARCHAR(20),
                        correlation_strategy TEXT,
                        correlation_score FLOAT,
                        correlation_details JSONB,
                        alert_metadata JSONB,
                        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_incident_alerts_incident_id ON incident_alerts(incident_id);
                    CREATE INDEX IF NOT EXISTS idx_incident_alerts_source ON incident_alerts(source_type, source_alert_id);
                    CREATE INDEX IF NOT EXISTS idx_incident_alerts_incident_received ON incident_alerts(incident_id, received_at);
                """,
                "incident_suggestions": """
                    CREATE TABLE IF NOT EXISTS incident_suggestions (
                        id SERIAL PRIMARY KEY,
                        incident_id UUID REFERENCES incidents(id) ON DELETE CASCADE,
                        title TEXT NOT NULL,
                        description TEXT,
                        type VARCHAR(20),
                        risk VARCHAR(20),
                        command TEXT,
                        -- Fields for fix-type suggestions (code changes)
                        file_path TEXT,
                        original_content TEXT,
                        suggested_content TEXT,
                        user_edited_content TEXT,
                        repository TEXT,
                        pr_url TEXT,
                        pr_number INTEGER,
                        created_branch TEXT,
                        applied_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_incident_suggestions_incident_id ON incident_suggestions(incident_id);
                """,
                "incident_thoughts": """
                    CREATE TABLE IF NOT EXISTS incident_thoughts (
                        id SERIAL PRIMARY KEY,
                        incident_id UUID REFERENCES incidents(id) ON DELETE CASCADE,
                        timestamp TIMESTAMP NOT NULL,
                        content TEXT NOT NULL,
                        thought_type VARCHAR(20),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    
                    CREATE INDEX IF NOT EXISTS idx_incident_thoughts_incident_id ON incident_thoughts(incident_id);
                """,
                "incident_citations": """
                    CREATE TABLE IF NOT EXISTS incident_citations (
                        id SERIAL PRIMARY KEY,
                        incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
                        citation_key VARCHAR(10) NOT NULL,
                        tool_name VARCHAR(255),
                        command TEXT,
                        output TEXT NOT NULL,
                        executed_at TIMESTAMP,
                        duration_ms INTEGER,
                        status VARCHAR(20) DEFAULT 'success',
                        error_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(incident_id, citation_key)
                    );

                    CREATE INDEX IF NOT EXISTS idx_incident_citations_incident_id ON incident_citations(incident_id);
                """,
                "execution_steps": """
                    CREATE TABLE IF NOT EXISTS execution_steps (
                        id SERIAL PRIMARY KEY,
                        incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
                        session_id VARCHAR(50) NOT NULL,
                        org_id VARCHAR(255),
                        step_index INTEGER NOT NULL,
                        tool_name VARCHAR(255) NOT NULL,
                        tool_call_id VARCHAR(255),
                        tool_input JSONB DEFAULT '{}'::jsonb,
                        tool_output TEXT,
                        status VARCHAR(20) NOT NULL DEFAULT 'running',
                        started_at TIMESTAMPTZ NOT NULL,
                        completed_at TIMESTAMPTZ,
                        duration_ms INTEGER,
                        error_message TEXT,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_execution_steps_incident ON execution_steps(incident_id, step_index);
                    CREATE INDEX IF NOT EXISTS idx_execution_steps_session ON execution_steps(session_id);
                    CREATE INDEX IF NOT EXISTS idx_execution_steps_org_time ON execution_steps(org_id, started_at);
                """,
                "rca_notification_emails": """
                    CREATE TABLE IF NOT EXISTS rca_notification_emails (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(1000) NOT NULL,
                        org_id VARCHAR(255),
                        email VARCHAR(255) NOT NULL,
                        is_verified BOOLEAN DEFAULT FALSE,
                        is_enabled BOOLEAN DEFAULT TRUE,
                        verification_code VARCHAR(6),
                        verification_code_expires_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        verified_at TIMESTAMP,
                        UNIQUE(user_id, email),
                        UNIQUE(org_id, email)
                    );

                    CREATE INDEX IF NOT EXISTS idx_rca_emails_user_id ON rca_notification_emails(user_id);
                    CREATE INDEX IF NOT EXISTS idx_rca_emails_verified ON rca_notification_emails(user_id, is_verified);
                    CREATE INDEX IF NOT EXISTS idx_rca_emails_enabled ON rca_notification_emails(user_id, is_verified, is_enabled);
                """,
                "splunk_alerts": """
                    CREATE TABLE IF NOT EXISTS splunk_alerts (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        alert_id VARCHAR(255),
                        alert_title TEXT,
                        alert_state VARCHAR(50),
                        search_name TEXT,
                        search_query TEXT,
                        result_count INTEGER,
                        severity VARCHAR(50),
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_splunk_alerts_user_id ON splunk_alerts(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_splunk_alerts_state ON splunk_alerts(alert_state);
                    CREATE INDEX IF NOT EXISTS idx_splunk_alerts_received_at ON splunk_alerts(received_at DESC);
                """,
                "incidentio_alerts": """
                    CREATE TABLE IF NOT EXISTS incidentio_alerts (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        incident_id VARCHAR(255),
                        incident_name TEXT,
                        incident_status VARCHAR(100),
                        severity VARCHAR(50),
                        incident_type VARCHAR(255),
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_incidentio_alerts_org_incident
                        ON incidentio_alerts(org_id, incident_id);
                    CREATE INDEX IF NOT EXISTS idx_incidentio_alerts_user_id
                        ON incidentio_alerts(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_incidentio_alerts_status
                        ON incidentio_alerts(incident_status);
                    CREATE INDEX IF NOT EXISTS idx_incidentio_alerts_severity
                        ON incidentio_alerts(severity);
                """,
                "jenkins_deployment_events": """
                    CREATE TABLE IF NOT EXISTS jenkins_deployment_events (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        event_type VARCHAR(50) DEFAULT 'deployment',
                        service VARCHAR(255),
                        environment VARCHAR(100),
                        result VARCHAR(50),
                        build_number INTEGER,
                        build_url TEXT,
                        commit_sha VARCHAR(64),
                        branch VARCHAR(255),
                        repository TEXT,
                        deployer VARCHAR(255),
                        duration_ms BIGINT,
                        job_name TEXT,
                        trace_id VARCHAR(64),
                        span_id VARCHAR(32),
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        provider VARCHAR(50) DEFAULT 'jenkins'
                    );

                    CREATE INDEX IF NOT EXISTS idx_jenkins_deploy_user ON jenkins_deployment_events(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_jenkins_deploy_service ON jenkins_deployment_events(service, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_jenkins_deploy_commit ON jenkins_deployment_events(commit_sha);
                    CREATE INDEX IF NOT EXISTS idx_jenkins_deploy_trace ON jenkins_deployment_events(trace_id) WHERE trace_id IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_jenkins_deploy_dedup ON jenkins_deployment_events(user_id, COALESCE(job_name, ''), COALESCE(build_number, -1));
                """,
                "spinnaker_deployment_events": """
                    CREATE TABLE IF NOT EXISTS spinnaker_deployment_events (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        event_type VARCHAR(50) DEFAULT 'pipeline',
                        application VARCHAR(255),
                        pipeline_name VARCHAR(255),
                        execution_id VARCHAR(255),
                        execution_url TEXT,
                        status VARCHAR(50),
                        trigger_type VARCHAR(100),
                        trigger_user VARCHAR(255),
                        start_time TIMESTAMP,
                        end_time TIMESTAMP,
                        duration_ms BIGINT,
                        stages JSONB,
                        parameters JSONB,
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_spinnaker_deploy_user ON spinnaker_deployment_events(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_spinnaker_deploy_app ON spinnaker_deployment_events(application, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_spinnaker_deploy_exec ON spinnaker_deployment_events(execution_id);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_spinnaker_deploy_dedup ON spinnaker_deployment_events(user_id, COALESCE(application, ''), COALESCE(execution_id, ''));
                """,
                "dynatrace_problems": """
                    CREATE TABLE IF NOT EXISTS dynatrace_problems (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        problem_id VARCHAR(255),
                        pid VARCHAR(255),
                        problem_title TEXT,
                        problem_state VARCHAR(50),
                        severity VARCHAR(50),
                        impact VARCHAR(50),
                        impacted_entity TEXT,
                        problem_url TEXT,
                        tags TEXT,
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_dynatrace_problems_user_id ON dynatrace_problems(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_dynatrace_problems_state ON dynatrace_problems(problem_state);
                    CREATE INDEX IF NOT EXISTS idx_dynatrace_problems_received_at ON dynatrace_problems(received_at DESC);
                """,
                "bigpanda_events": """
                    CREATE TABLE IF NOT EXISTS bigpanda_events (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        event_type VARCHAR(100),
                        incident_id VARCHAR(255),
                        incident_title TEXT,
                        incident_status VARCHAR(50),
                        incident_severity VARCHAR(100),
                        primary_property VARCHAR(255),
                        secondary_property VARCHAR(255),
                        source_system VARCHAR(255),
                        child_alert_count INTEGER DEFAULT 0,
                        payload JSONB NOT NULL,
                        received_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_bigpanda_events_user_id ON bigpanda_events(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_bigpanda_events_incident_id ON bigpanda_events(incident_id);
                    CREATE INDEX IF NOT EXISTS idx_bigpanda_events_status ON bigpanda_events(incident_status);
                    CREATE INDEX IF NOT EXISTS idx_bigpanda_events_received_at ON bigpanda_events(received_at DESC);
                """,
                "kubectl_agent_tokens": """
                    CREATE TABLE IF NOT EXISTS kubectl_agent_tokens (
                        id SERIAL PRIMARY KEY,
                        token VARCHAR(128) UNIQUE NOT NULL,
                        user_id TEXT NOT NULL,
                        org_id VARCHAR(255),
                        cluster_name TEXT,
                        cluster_id TEXT,
                        created_at TIMESTAMP DEFAULT NOW(),
                        last_connected_at TIMESTAMP,
                        expires_at TIMESTAMP,
                        status TEXT DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'expired')),
                        notes TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_kubectl_tokens_user ON kubectl_agent_tokens(user_id);
                    CREATE INDEX IF NOT EXISTS idx_kubectl_tokens_token ON kubectl_agent_tokens(token);
                    CREATE INDEX IF NOT EXISTS idx_kubectl_tokens_status ON kubectl_agent_tokens(status);
                """,
                "mcp_tokens": """
                    CREATE TABLE IF NOT EXISTS mcp_tokens (
                        id SERIAL PRIMARY KEY,
                        token VARCHAR(128) UNIQUE NOT NULL,
                        user_id TEXT NOT NULL,
                        org_id VARCHAR(255),
                        name TEXT,
                        created_at TIMESTAMP DEFAULT NOW(),
                        last_used_at TIMESTAMP,
                        expires_at TIMESTAMP,
                        status TEXT DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'expired'))
                    );

                    CREATE INDEX IF NOT EXISTS idx_mcp_tokens_token ON mcp_tokens(token);
                    CREATE INDEX IF NOT EXISTS idx_mcp_tokens_user ON mcp_tokens(user_id);
                """,
                "active_kubectl_connections": """
                    CREATE TABLE IF NOT EXISTS active_kubectl_connections (
                        id SERIAL PRIMARY KEY,
                        token VARCHAR(128) NOT NULL,
                        cluster_id TEXT NOT NULL UNIQUE,
                        connected_at TIMESTAMP DEFAULT NOW(),
                        last_heartbeat TIMESTAMP DEFAULT NOW(),
                        agent_version TEXT,
                        k8s_context TEXT,
                        status TEXT DEFAULT 'active' CHECK (status IN ('active', 'stale'))
                    );

                    CREATE INDEX IF NOT EXISTS idx_kubectl_connections_token ON active_kubectl_connections(token);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_kubectl_connections_cluster_id ON active_kubectl_connections(cluster_id);
                """,
                "users": """
                    CREATE TABLE IF NOT EXISTS users (
                        id VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
                        email VARCHAR(255) NOT NULL UNIQUE,
                        password_hash VARCHAR(255) NOT NULL,
                        name VARCHAR(255),
                        org_id VARCHAR(255),
                        must_change_password BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    );

                    CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
                """,
                "organizations": """
                    CREATE TABLE IF NOT EXISTS organizations (
                        id VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
                        name VARCHAR(255) NOT NULL,
                        slug VARCHAR(255) NOT NULL UNIQUE,
                        created_by VARCHAR(255) REFERENCES users(id),
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    );

                    CREATE INDEX IF NOT EXISTS idx_organizations_slug ON organizations(slug);
                """,
                "org_command_policies": """
                    CREATE TABLE IF NOT EXISTS org_command_policies (
                        id SERIAL PRIMARY KEY,
                        org_id VARCHAR(255) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                        mode VARCHAR(20) NOT NULL CHECK (mode IN ('allow', 'deny')),
                        pattern TEXT NOT NULL,
                        description TEXT,
                        priority INT DEFAULT 0,
                        enabled BOOLEAN DEFAULT true,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_by VARCHAR(255),
                        source VARCHAR(20) DEFAULT 'custom' NOT NULL,
                        UNIQUE(org_id, mode, pattern, source)
                    );
                    CREATE INDEX IF NOT EXISTS idx_ocp_org
                        ON org_command_policies(org_id);
                """,
                "org_invitations": """
                    CREATE TABLE IF NOT EXISTS org_invitations (
                        id VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
                        org_id VARCHAR(255) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                        email VARCHAR(255) NOT NULL,
                        role VARCHAR(50) DEFAULT 'viewer',
                        invited_by VARCHAR(255) NOT NULL REFERENCES users(id),
                        status VARCHAR(20) DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT NOW(),
                        expires_at TIMESTAMP DEFAULT (NOW() + INTERVAL '7 days'),
                        UNIQUE(org_id, email)
                    );

                    CREATE INDEX IF NOT EXISTS idx_org_invitations_org_id ON org_invitations(org_id);
                    CREATE INDEX IF NOT EXISTS idx_org_invitations_email ON org_invitations(email);
                """,
                "knowledge_base_memory": """
                    CREATE TABLE IF NOT EXISTS knowledge_base_memory (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(1000) NOT NULL,
                        org_id VARCHAR(255),
                        content TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, org_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_kb_memory_user_id ON knowledge_base_memory(user_id);
                    CREATE INDEX IF NOT EXISTS idx_kb_memory_org_id ON knowledge_base_memory(org_id, updated_at DESC);
                """,
                "knowledge_base_documents": """
                    CREATE TABLE IF NOT EXISTS knowledge_base_documents (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id VARCHAR(1000) NOT NULL,
                        org_id VARCHAR(255),
                        filename VARCHAR(500) NOT NULL,
                        original_filename VARCHAR(500) NOT NULL,
                        file_type VARCHAR(50) NOT NULL,
                        file_size_bytes BIGINT NOT NULL,
                        status VARCHAR(50) NOT NULL DEFAULT 'uploading',
                        error_message TEXT,
                        chunk_count INTEGER DEFAULT 0,
                        storage_path VARCHAR(1000),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, filename)
                    );

                    CREATE INDEX IF NOT EXISTS idx_kb_documents_user_id ON knowledge_base_documents(user_id);
                    CREATE INDEX IF NOT EXISTS idx_kb_documents_status ON knowledge_base_documents(status);
                """,
                "incident_feedback": """
                    CREATE TABLE IF NOT EXISTS incident_feedback (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
                        feedback_type VARCHAR(20) NOT NULL,
                        comment TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_incident_feedback_user_incident
                    ON incident_feedback(user_id, incident_id);

                    CREATE INDEX IF NOT EXISTS idx_incident_feedback_user_id ON incident_feedback(user_id);
                    CREATE INDEX IF NOT EXISTS idx_incident_feedback_type ON incident_feedback(feedback_type);
                """,
                "postmortems": """
                    CREATE TABLE IF NOT EXISTS postmortems (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        content TEXT,
                        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        confluence_page_id TEXT,
                        confluence_page_url TEXT,
                        confluence_exported_at TIMESTAMP,
                        notion_page_id TEXT,
                        notion_page_url TEXT,
                        notion_exported_at TIMESTAMP,
                        notion_database_id TEXT
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_postmortems_incident_id ON postmortems(incident_id);
                    CREATE UNIQUE INDEX IF NOT EXISTS postmortems_incident_id_unique ON postmortems(incident_id);
                    CREATE INDEX IF NOT EXISTS idx_postmortems_user_id ON postmortems(user_id);
                """,
                "incident_lifecycle_events": """
                    CREATE TABLE IF NOT EXISTS incident_lifecycle_events (
                        id SERIAL PRIMARY KEY,
                        incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
                        user_id VARCHAR(255) NOT NULL,
                        org_id VARCHAR(255),
                        event_type VARCHAR(50) NOT NULL,
                        previous_value VARCHAR(50),
                        new_value VARCHAR(50),
                        metadata JSONB DEFAULT '{}'::jsonb,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_lifecycle_incident ON incident_lifecycle_events(incident_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_lifecycle_user ON incident_lifecycle_events(user_id, created_at DESC);
                """,
                "audit_log": """
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id SERIAL PRIMARY KEY,
                        org_id VARCHAR(255) NOT NULL,
                        user_id VARCHAR(255) NOT NULL,
                        action VARCHAR(100) NOT NULL,
                        resource_type VARCHAR(100) NOT NULL,
                        resource_id VARCHAR(255),
                        detail JSONB DEFAULT '{}'::jsonb,
                        ip_address VARCHAR(45),
                        user_agent TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_audit_log_org_created ON audit_log(org_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(org_id, action);
                """,
            }

            # List of tables that should have RLS enabled and a policy applied.
            rls_tables = [
                "cloud_billing_usage",
                "user_tokens",
                "user_connections",
                "provider_metrics",
                "k8s_services",
                "k8s_pods",
                "k8s_nodes",
                "k8s_node_conditions",
                "k8s_deployments",
                "k8s_ingresses",
                "k8s_pod_metrics",
                "k8s_node_metrics",
                "deployments",
                "chat_sessions",
                "user_preferences",
                "deployment_tasks",
                "llm_usage_tracking",
                "rca_notification_emails",
                "kubectl_agent_tokens",
                "mcp_tokens",
                "user_manual_vms",
            ]

            # Tables with org_id NOT in this list (intentional):
            # - users: queried during login before org context is set; RLS would break auth
            # - audit_log: written via record_audit_event() which passes org_id explicitly;
            #   RLS would silently drop inserts when session org_id doesn't match or isn't set
            # - org_invitations: queried during invite/join flows before org context is set
            # - knowledge_base_documents, knowledge_base_memory: cleanup_stale_documents
            #   Celery task runs cross-org sweeps with no user context; needs SECURITY
            #   DEFINER function or BYPASSRLS role before RLS can be added
            rls_tables.append("workspaces")
            rls_tables.append("aurora_deployments")
            rls_tables.append("cloud_feed_metadata")
            rls_tables.append("cloud_ingestion_state")
            rls_tables.append("newrelic_events")
            rls_tables.append("pagerduty_events")

            # Add monitoring tables
            rls_tables.append("grafana_alerts")
            rls_tables.append("datadog_events")
            rls_tables.append("netdata_alerts")
            rls_tables.append("splunk_alerts")
            rls_tables.append("incidentio_alerts")
            rls_tables.append("bigpanda_events")
            rls_tables.append("jenkins_deployment_events")
            rls_tables.append("spinnaker_deployment_events")
            rls_tables.append("dynatrace_problems")
            rls_tables.append("opsgenie_events")

            # Add incidents table
            # Note: incident_suggestions and incident_thoughts are child tables with CASCADE DELETE
            # so they don't need RLS - incident_alerts is protected separately for safety
            rls_tables.append("incidents")
            rls_tables.append("incident_alerts")
            rls_tables.append("incident_feedback")
            rls_tables.append("postmortems")
            rls_tables.append("postmortem_exports")
            rls_tables.append("incident_lifecycle_events")
            rls_tables.append("github_connected_repos")
            rls_tables.append("execution_steps")
            rls_tables.append("org_command_policies")


            # Migration: Add rca_celery_task_id column to incidents table if it doesn't exist
            try:
                cursor.execute("""
                    ALTER TABLE incidents
                    ADD COLUMN IF NOT EXISTS rca_celery_task_id VARCHAR(255);
                """)
                conn.commit()
                logging.info(
                    "Added rca_celery_task_id column to incidents table (if not exists)."
                )
            except Exception as e:
                logging.warning(f"Error adding rca_celery_task_id column to incidents: {e}")
                conn.rollback()

            # Migration: Add merged_into_incident_id column to incidents table if it exists
            # This must run BEFORE table creation scripts because the incidents creation
            # script includes a CREATE INDEX on this column, which fails if the table
            # exists but the column doesn't.
            try:
                cursor.execute("""
                    ALTER TABLE incidents
                    ADD COLUMN IF NOT EXISTS merged_into_incident_id UUID REFERENCES incidents(id) ON DELETE SET NULL;
                """)
                conn.commit()
                logging.info(
                    "Ensured merged_into_incident_id column exists on incidents table."
                )
            except Exception as e:
                # Table may not exist yet (new install) - that's fine,
                # CREATE TABLE will include the column.
                logging.warning(f"Error adding merged_into_incident_id column to incidents: {e}")
                conn.rollback()

            # Execute table creation scripts
            for table_name, create_script in create_tables.items():
                cursor.execute(create_script)
                logging.info(f"Table '{table_name}' initialized successfully.")

            # Migration: ensure incident_alerts.user_id exists and is backfilled
            try:
                cursor.execute(
                    "ALTER TABLE incident_alerts ADD COLUMN IF NOT EXISTS user_id VARCHAR(1000);"
                )
                cursor.execute(
                    "ALTER TABLE incident_alerts ADD COLUMN IF NOT EXISTS received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;"
                )
                cursor.execute(
                    """
                    UPDATE incident_alerts ia
                    SET user_id = i.user_id
                    FROM incidents i
                    WHERE ia.incident_id = i.id
                      AND ia.user_id IS NULL;
                    """
                )
                cursor.execute(
                    "ALTER TABLE incident_alerts ALTER COLUMN user_id SET NOT NULL;"
                )
                conn.commit()
                logging.info(
                    "Ensured user_id column exists and is populated on incident_alerts table."
                )
            except Exception as e:
                logging.warning(f"Error ensuring user_id on incident_alerts table: {e}")
                conn.rollback()

            # Add read_only_role_arn to user_connections table for single source of truth
            try:
                cursor.execute(
                    "ALTER TABLE user_connections ADD COLUMN IF NOT EXISTS read_only_role_arn VARCHAR(512);"
                )
                conn.commit()
                logging.info(
                    "Ensured read_only_role_arn column exists on user_connections table."
                )
            except Exception as e:
                logging.warning(
                    f"Error ensuring read_only_role_arn column in user_connections: {e}"
                )
                conn.rollback()

            # Add region column to user_connections for multi-account support
            try:
                cursor.execute(
                    "ALTER TABLE user_connections ADD COLUMN IF NOT EXISTS region VARCHAR(50);"
                )
                conn.commit()
                logging.info(
                    "Ensured region column exists on user_connections table."
                )
            except Exception as e:
                logging.error(
                    "FATAL: Failed to ensure region column in user_connections: %s", e
                )
                conn.rollback()
                raise

            # Add workspace_id to user_connections so credential refresh can
            # look up the external_id without a fragile JOIN through workspaces.
            try:
                cursor.execute(
                    "ALTER TABLE user_connections ADD COLUMN IF NOT EXISTS workspace_id VARCHAR(255);"
                )
                conn.commit()
                logging.info(
                    "Ensured workspace_id column exists on user_connections table."
                )
            except Exception as e:
                logging.error(
                    "FATAL: Failed to ensure workspace_id column in user_connections: %s", e
                )
                conn.rollback()
                raise

            # Add stateless migration columns to user_tokens if they don't exist
            try:
                cursor.execute("""
                    ALTER TABLE user_tokens 
                    ADD COLUMN IF NOT EXISTS session_data JSONB,
                    ADD COLUMN IF NOT EXISTS last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT true;
                """)
                logging.info(
                    "Added stateless migration columns to user_tokens table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding stateless columns to user_tokens: {e}")
                conn.rollback()

            # Migration: Add ui_state column to chat_sessions if it doesn't exist
            try:
                cursor.execute("""
                    ALTER TABLE chat_sessions 
                    ADD COLUMN IF NOT EXISTS ui_state JSONB DEFAULT '{}'::jsonb;
                """)
                logging.info(
                    "Added ui_state column to chat_sessions table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding ui_state column: {e}")
                conn.rollback()

            # Migration: Add llm_context_history column to chat_sessions if it doesn't exist
            try:
                cursor.execute("""
                    ALTER TABLE chat_sessions 
                    ADD COLUMN IF NOT EXISTS llm_context_history JSONB DEFAULT '[]'::jsonb;
                """)
                logging.info(
                    "Added llm_context_history column to chat_sessions table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding llm_context_history column: {e}")
                conn.rollback()

            # Migration: Add status column to chat_sessions if it doesn't exist
            try:
                cursor.execute("""
                    ALTER TABLE chat_sessions 
                    ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'active';
                """)
                logging.info(
                    "Added status column to chat_sessions table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding status column: {e}")
                conn.rollback()

            # Migration: Add incident_id column to chat_sessions if it doesn't exist
            try:
                cursor.execute("""
                    ALTER TABLE chat_sessions 
                    ADD COLUMN IF NOT EXISTS incident_id UUID;
                """)
                logging.info(
                    "Added incident_id column to chat_sessions table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding incident_id column: {e}")
                conn.rollback()

            # Migration: Add foreign key constraint for incident_id if it doesn't exist
            try:
                cursor.execute("""
                    DO $$ 
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint 
                            WHERE conname = 'chat_sessions_incident_id_fkey'
                        ) THEN
                            ALTER TABLE chat_sessions 
                            ADD CONSTRAINT chat_sessions_incident_id_fkey 
                            FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE SET NULL;
                        END IF;
                    END $$;
                """)
                logging.info(
                    "Added foreign key constraint for incident_id on chat_sessions table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding incident_id foreign key constraint: {e}")
                conn.rollback()

            # Migration: Add index for incident_id if it doesn't exist
            try:
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_chat_sessions_incident_id 
                    ON chat_sessions(incident_id);
                """)
                logging.info(
                    "Added index for incident_id on chat_sessions table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding incident_id index: {e}")
                conn.rollback()

            # Migration: Add pending_turn column (live HITL state, separate from
            # the append-only messages history). Cleared by the command gate
            # once the user resolves the confirmation. Rehydrated as a
            # synthetic tail card on session load.
            try:
                cursor.execute("""
                    ALTER TABLE chat_sessions
                    ADD COLUMN IF NOT EXISTS pending_turn JSONB;
                """)
                logging.info(
                    "Added pending_turn column to chat_sessions table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding pending_turn column: {e}")
                conn.rollback()

            # Migration: Add surcharge fields to llm_usage_tracking table if they don't exist
            try:
                cursor.execute("""
                    ALTER TABLE llm_usage_tracking 
                    ADD COLUMN IF NOT EXISTS surcharge_rate DECIMAL(5,4) DEFAULT 0.0000;
                """)
                cursor.execute("""
                    ALTER TABLE llm_usage_tracking 
                    ADD COLUMN IF NOT EXISTS surcharge_amount DECIMAL(10,6) GENERATED ALWAYS AS (estimated_cost * surcharge_rate) STORED;
                """)
                cursor.execute("""
                    ALTER TABLE llm_usage_tracking 
                    ADD COLUMN IF NOT EXISTS total_cost_with_surcharge DECIMAL(10,6) GENERATED ALWAYS AS (estimated_cost * (1 + surcharge_rate)) STORED;
                """)
                logging.info(
                    "Added surcharge fields to llm_usage_tracking table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(
                    f"Error adding surcharge fields to llm_usage_tracking: {e}"
                )
                conn.rollback()

            # Migration: Zero out surcharge_rate (markup removed, raw provider costs only)
            try:
                cursor.execute("""
                    ALTER TABLE llm_usage_tracking
                    ALTER COLUMN surcharge_rate SET DEFAULT 0.0000;
                """)
                cursor.execute("""
                    SELECT EXISTS(
                        SELECT 1 FROM llm_usage_tracking
                        WHERE surcharge_rate != 0.0000
                        LIMIT 1
                    );
                """)
                has_nonzero = cursor.fetchone()[0]
                if has_nonzero:
                    cursor.execute("""
                        UPDATE llm_usage_tracking
                        SET surcharge_rate = 0.0000
                        WHERE surcharge_rate != 0.0000;
                    """)
                    logging.info(f"Zeroed surcharge_rate on {cursor.rowcount} llm_usage_tracking rows.")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error zeroing surcharge_rate: {e}")
                conn.rollback()

            # Migration: Backfill org_id on llm_usage_tracking from users table
            try:
                cursor.execute("""
                    SELECT EXISTS(
                        SELECT 1 FROM llm_usage_tracking
                        WHERE org_id IS NULL
                        LIMIT 1
                    );
                """)
                has_null_org = cursor.fetchone()[0]
                if has_null_org:
                    cursor.execute("""
                        UPDATE llm_usage_tracking lut
                        SET org_id = u.org_id
                        FROM users u
                        WHERE lut.user_id = u.id::text
                          AND lut.org_id IS NULL
                          AND u.org_id IS NOT NULL;
                    """)
                    updated = cursor.rowcount
                    if updated > 0:
                        logging.info(f"Backfilled org_id on {updated} llm_usage_tracking rows.")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error backfilling org_id on llm_usage_tracking: {e}")
                conn.rollback()

            # Migration: Add secret_ref column to user_tokens for Vault integration
            try:
                cursor.execute(
                    """
                    ALTER TABLE user_tokens
                    ADD COLUMN IF NOT EXISTS secret_ref VARCHAR(512);
                    """
                )
                logging.info(
                    "Added secret_ref column to user_tokens table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding secret_ref column to user_tokens: {e}")
                conn.rollback()

            # Migration: Make token_data column nullable for Vault migration
            try:
                cursor.execute(
                    """
                    ALTER TABLE user_tokens
                    ALTER COLUMN token_data DROP NOT NULL;
                    """
                )
                logging.info("Made token_data column nullable in user_tokens table.")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error making token_data nullable: {e}")
                conn.rollback()

            # Migration: Add email column to user_tokens for GCP provider
            try:
                cursor.execute(
                    """
                    ALTER TABLE user_tokens
                    ADD COLUMN IF NOT EXISTS email VARCHAR(255);
                    """
                )
                logging.info("Added email column to user_tokens table (if not exists).")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding email column to user_tokens: {e}")
                conn.rollback()

            # Add alert_metadata column to incidents table for provider-specific fields
            try:
                cursor.execute(
                    """
                    ALTER TABLE incidents
                    ADD COLUMN IF NOT EXISTS alert_metadata JSONB DEFAULT '{}'::jsonb;
                    """
                )
                logging.info(
                    "Added alert_metadata column to incidents table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding alert_metadata column to incidents: {e}")
                conn.rollback()

            # Migration: Add provider column to jenkins_deployment_events for multi-CI support
            try:
                cursor.execute(
                    """
                    ALTER TABLE jenkins_deployment_events
                    ADD COLUMN IF NOT EXISTS provider VARCHAR(50) DEFAULT 'jenkins';
                    """
                )
                logging.info("Added provider column to jenkins_deployment_events table (if not exists).")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding provider column to jenkins_deployment_events: {e}")
                conn.rollback()

            try:
                cursor.execute(
                    """
                    ALTER TABLE user_manual_vms
                    ADD COLUMN IF NOT EXISTS ssh_username VARCHAR(255);
                    """
                )
                logging.info(
                    "Ensured ssh_username column exists on user_manual_vms table."
                )
                conn.commit()
            except Exception as e:
                logging.warning(
                    f"Error ensuring ssh_username column on user_manual_vms: {e}"
                )
            try:
                cursor.execute(
                    """
                    ALTER TABLE user_manual_vms
                    ADD COLUMN IF NOT EXISTS connection_verified BOOLEAN DEFAULT FALSE;
                    """
                )
                logging.info(
                    "Added connection_verified column to user_manual_vms table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logging.warning(
                    f"Error adding connection_verified column to user_manual_vms: {e}"
                )
            # Add slack_message_ts column to incidents table for Slack message updates
            try:
                cursor.execute(
                    """
                    ALTER TABLE incidents
                    ADD COLUMN IF NOT EXISTS slack_message_ts VARCHAR(50);
                    """
                )
                conn.commit()
            except Exception as e:
                logging.warning(
                    f"Error adding slack_message_ts column to incidents: {e}"
                )
                conn.rollback()

            # Add google_chat_message_name column to incidents table for Google Chat message updates
            try:
                cursor.execute(
                    """
                    ALTER TABLE incidents
                    ADD COLUMN IF NOT EXISTS google_chat_message_name VARCHAR(255);
                    """
                )
                conn.commit()
            except Exception as e:
                logging.error(
                    f"Failed to add google_chat_message_name column to incidents: {e}"
                )
                conn.rollback()

            # Migration: Add active_tab column to incidents for UI state persistence
            try:
                cursor.execute(
                    """
                    ALTER TABLE incidents
                    ADD COLUMN IF NOT EXISTS active_tab VARCHAR(10) DEFAULT 'thoughts';
                    """
                )
                logging.info(
                    "Added active_tab column to incidents table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding active_tab column to incidents: {e}")
                conn.rollback()

            # Add correlated_alert_count column to incidents table
            try:
                cursor.execute("""
                    ALTER TABLE incidents
                    ADD COLUMN IF NOT EXISTS correlated_alert_count INTEGER DEFAULT 0;
                """)
                logging.info(
                    "Added correlated_alert_count column to incidents table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(
                    f"Error adding correlated_alert_count column to incidents: {e}"
                )
                conn.rollback()

            # Add affected_services column to incidents table
            try:
                cursor.execute("""
                    ALTER TABLE incidents
                    ADD COLUMN IF NOT EXISTS affected_services TEXT[] DEFAULT '{}';
                """)
                logging.info(
                    "Added affected_services column to incidents table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(
                    f"Error adding affected_services column to incidents: {e}"
                )
                conn.rollback()

            # Add visualization columns to incidents table
            try:
                cursor.execute("""
                    ALTER TABLE incidents
                    ADD COLUMN IF NOT EXISTS visualization_code TEXT,
                    ADD COLUMN IF NOT EXISTS visualization_updated_at TIMESTAMPTZ;
                """)
                logging.info(
                    "Added visualization_code and visualization_updated_at columns to incidents table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(
                    f"Error adding visualization columns to incidents: {e}"
                )
                conn.rollback()

            # Add fix-type columns to incident_suggestions for code fix suggestions
            try:
                cursor.execute(
                    """
                    ALTER TABLE incident_suggestions
                    ADD COLUMN IF NOT EXISTS file_path TEXT,
                    ADD COLUMN IF NOT EXISTS original_content TEXT,
                    ADD COLUMN IF NOT EXISTS suggested_content TEXT,
                    ADD COLUMN IF NOT EXISTS user_edited_content TEXT,
                    ADD COLUMN IF NOT EXISTS repository TEXT,
                    ADD COLUMN IF NOT EXISTS pr_url TEXT,
                    ADD COLUMN IF NOT EXISTS pr_number INTEGER,
                    ADD COLUMN IF NOT EXISTS created_branch TEXT,
                    ADD COLUMN IF NOT EXISTS applied_at TIMESTAMP;
                    """
                )
                logging.info(
                    "Added fix-type columns to incident_suggestions table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(
                    f"Error adding fix-type columns to incident_suggestions: {e}"
                )
                conn.rollback()

            # Add execution-tracking columns to incident_suggestions
            try:
                cursor.execute(
                    """
                    ALTER TABLE incident_suggestions
                    ADD COLUMN IF NOT EXISTS executed_at TIMESTAMP,
                    ADD COLUMN IF NOT EXISTS execution_session_id UUID,
                    ADD COLUMN IF NOT EXISTS execution_status VARCHAR(20);
                    """
                )
                logging.info(
                    "Added execution-tracking columns to incident_suggestions table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(
                    f"Error adding execution-tracking columns to incident_suggestions: {e}"
                )
                conn.rollback()

            # Migration: Create postmortems table if it doesn't exist
            # Note: 'resolved' is now a valid incident status value.
            # The incidents.status column is VARCHAR so no ALTER TABLE is needed.
            try:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS postmortems (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
                        user_id VARCHAR(255) NOT NULL,
                        content TEXT,
                        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        confluence_page_id TEXT,
                        confluence_page_url TEXT,
                        confluence_exported_at TIMESTAMP,
                        notion_page_id TEXT,
                        notion_page_url TEXT,
                        notion_exported_at TIMESTAMP,
                        notion_database_id TEXT
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_postmortems_incident_id ON postmortems(incident_id);
                    CREATE INDEX IF NOT EXISTS idx_postmortems_user_id ON postmortems(user_id);
                """)
                logging.info(
                    "Created postmortems table (if not exists)."
                )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error creating postmortems table: {e}")
                conn.rollback()

            # Create indexes for performance
            indexes = [
                "CREATE INDEX IF NOT EXISTS idx_user_tokens_last_activity ON user_tokens(last_activity);",
                "CREATE INDEX IF NOT EXISTS idx_user_tokens_active ON user_tokens(user_id, is_active);",
                "CREATE INDEX IF NOT EXISTS idx_user_tokens_slack_team ON user_tokens(provider, subscription_id) WHERE provider = 'slack' AND subscription_id IS NOT NULL;",
                "CREATE INDEX IF NOT EXISTS idx_user_tokens_google_chat_domain ON user_tokens(provider, subscription_name) WHERE provider = 'google_chat' AND subscription_name IS NOT NULL;",
                "CREATE INDEX IF NOT EXISTS idx_user_manual_vms_user_id ON user_manual_vms(user_id);",
                "CREATE INDEX IF NOT EXISTS idx_user_manual_vms_key ON user_manual_vms(user_id, ssh_key_id);",
                "CREATE INDEX IF NOT EXISTS idx_user_manual_vms_connection_verified ON user_manual_vms(user_id, connection_verified);",
                "CREATE INDEX IF NOT EXISTS idx_user_preferences_user_key ON user_preferences(user_id, preference_key);",
                "CREATE INDEX IF NOT EXISTS idx_aurora_deployments_user_id ON aurora_deployments(user_id);",
                "CREATE INDEX IF NOT EXISTS idx_aurora_deployments_project_id ON aurora_deployments(project_id);",
                "CREATE INDEX IF NOT EXISTS idx_aurora_deployments_deployment_id ON aurora_deployments(deployment_id);",
                "CREATE INDEX IF NOT EXISTS idx_aurora_deployments_status ON aurora_deployments(status);",
                "CREATE INDEX IF NOT EXISTS idx_deployment_tasks_user ON deployment_tasks(user_id);",
                "CREATE INDEX IF NOT EXISTS idx_deployment_tasks_task_id ON deployment_tasks(task_id);",
                "CREATE INDEX IF NOT EXISTS idx_deployment_tasks_status ON deployment_tasks(status);",
                "CREATE INDEX IF NOT EXISTS idx_deployment_tasks_updated_at ON deployment_tasks(updated_at);",
                "CREATE INDEX IF NOT EXISTS idx_llm_usage_user_timestamp ON llm_usage_tracking(user_id, timestamp);",
                "CREATE INDEX IF NOT EXISTS idx_llm_usage_session ON llm_usage_tracking(session_id);",
                "CREATE INDEX IF NOT EXISTS idx_llm_usage_model ON llm_usage_tracking(model_name);",
                "CREATE INDEX IF NOT EXISTS idx_llm_usage_timestamp ON llm_usage_tracking(timestamp);",
            ]

            for index_sql in indexes:
                try:
                    cursor.execute(index_sql)
                    logging.info(
                        f"Index created: {index_sql.split()[5]}"
                    )  # Extract index name
                except Exception as e:
                    logging.warning(f"Error creating index: {e}")
                    conn.rollback()

            # Migration: Add Jira columns to postmortems table
            try:
                cursor.execute("""
                    ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS jira_issue_id TEXT;
                    ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS jira_issue_key TEXT;
                    ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS jira_issue_url TEXT;
                    ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS jira_exported_at TIMESTAMP;
                """)
                logging.info("Added Jira columns to postmortems table (if not exist).")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding Jira columns to postmortems: {e}")
                conn.rollback()

            # Migration: Add Notion columns to postmortems table
            try:
                cursor.execute("""
                    ALTER TABLE postmortems
                        ADD COLUMN IF NOT EXISTS notion_page_id TEXT,
                        ADD COLUMN IF NOT EXISTS notion_page_url TEXT,
                        ADD COLUMN IF NOT EXISTS notion_exported_at TIMESTAMP,
                        ADD COLUMN IF NOT EXISTS notion_database_id TEXT;
                """)
                logging.info("Added Notion columns to postmortems table (if not exist).")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding Notion columns to postmortems: {e}")
                conn.rollback()

            # Migration: Normalized postmortem_exports table — one row per
            # (postmortem, destination) instead of per-destination columns.
            # Future destinations (Linear, Slack Canvas, GDocs) are inserts,
            # not ALTER TABLEs.
            try:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS postmortem_exports (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        postmortem_id UUID NOT NULL REFERENCES postmortems(id) ON DELETE CASCADE,
                        org_id VARCHAR(255) NOT NULL,
                        destination VARCHAR(50) NOT NULL,
                        external_id TEXT,
                        external_key TEXT,
                        external_url TEXT,
                        external_database_id TEXT,
                        exported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (postmortem_id, destination)
                    );
                    CREATE INDEX IF NOT EXISTS idx_postmortem_exports_postmortem
                        ON postmortem_exports(postmortem_id);
                    CREATE INDEX IF NOT EXISTS idx_postmortem_exports_org_dest
                        ON postmortem_exports(org_id, destination);
                """)
                logging.info("Created postmortem_exports table (if not exists).")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error creating postmortem_exports table: {e}")
                conn.rollback()

            # Backfill postmortem_exports from legacy columns (idempotent)
            try:
                cursor.execute("""
                    INSERT INTO postmortem_exports (postmortem_id, org_id, destination, external_id, external_url, exported_at)
                    SELECT id, org_id, 'confluence', confluence_page_id, confluence_page_url, confluence_exported_at
                    FROM postmortems
                    WHERE confluence_page_id IS NOT NULL AND org_id IS NOT NULL
                    ON CONFLICT (postmortem_id, destination) DO NOTHING;

                    INSERT INTO postmortem_exports (postmortem_id, org_id, destination, external_id, external_key, external_url, exported_at)
                    SELECT id, org_id, 'jira', jira_issue_id, jira_issue_key, jira_issue_url, jira_exported_at
                    FROM postmortems
                    WHERE jira_issue_id IS NOT NULL AND org_id IS NOT NULL
                    ON CONFLICT (postmortem_id, destination) DO NOTHING;

                    INSERT INTO postmortem_exports (postmortem_id, org_id, destination, external_id, external_url, external_database_id, exported_at)
                    SELECT id, org_id, 'notion', notion_page_id, notion_page_url, notion_database_id, notion_exported_at
                    FROM postmortems
                    WHERE notion_page_id IS NOT NULL AND org_id IS NOT NULL
                    ON CONFLICT (postmortem_id, destination) DO NOTHING;
                """)
                logging.info("Backfilled postmortem_exports from legacy columns.")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error backfilling postmortem_exports: {e}")
                conn.rollback()

            # Migration: Add resolved_at, alert_fired_at, and investigation_started_at
            # columns to incidents table.
            #   - resolved_at:             when the incident was actually resolved
            #   - alert_fired_at:          when the upstream system raised the alert
            #   - investigation_started_at: when Aurora's RCA worker actually began work
            #                              (used to compute pickup latency / MTTD)
            try:
                cursor.execute("""
                    ALTER TABLE incidents
                    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP,
                    ADD COLUMN IF NOT EXISTS alert_fired_at TIMESTAMP,
                    ADD COLUMN IF NOT EXISTS investigation_started_at TIMESTAMP;
                """)
                logging.info("Added resolved_at, alert_fired_at, and investigation_started_at columns to incidents table (if not exist).")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error adding resolved_at/alert_fired_at/investigation_started_at to incidents: {e}")
                conn.rollback()

            # NOTE: We intentionally do NOT backfill resolved_at from updated_at:
            # updated_at is bumped on every PATCH (summary, aurora status, active-tab,
            # etc.), so it would stamp the last metadata change rather than the true
            # resolution time and skew MTTR for legacy incidents. Leave legacy
            # resolved_at as NULL — those rows will simply not contribute to MTTR.

            # Indexes for SRE metrics queries. These queries are org-scoped via
            # RLS (SET myapp.current_org_id) and do not filter by user_id in their
            # WHERE clauses, so user_id as a leading column would not contribute
            # to selectivity. Index only the columns actually used as predicates.
            # We DROP any earlier (user_id, ...) variants so dev/staging environments
            # that saw the first definition get the corrected indexes on next boot.
            try:
                cursor.execute("""
                    DROP INDEX IF EXISTS idx_incidents_resolved_at;
                    DROP INDEX IF EXISTS idx_incidents_service_started;

                    CREATE INDEX IF NOT EXISTS idx_incidents_resolved_at
                    ON incidents(resolved_at DESC) WHERE resolved_at IS NOT NULL;

                    CREATE INDEX IF NOT EXISTS idx_incidents_service_started
                    ON incidents(alert_service, started_at DESC);
                """)
                logging.info("Created SRE metrics indexes on incidents table.")
                conn.commit()
            except Exception as e:
                logging.warning(f"Error creating SRE metrics indexes: {e}")
                conn.rollback()

            # View creation moved to after org_id migration (see below)

            # Early migration: ensure org_id column exists on all tables
            # before RLS policies try to reference it
            _org_id_tables = list(set(rls_tables + [
                "users", "workspaces", "aurora_deployments",
                "cloud_feed_metadata", "cloud_ingestion_state",
                "pagerduty_events", "opsgenie_events", "knowledge_base_memory",
                "knowledge_base_documents",
            ]))
            for tbl in _org_id_tables:
                try:
                    cursor.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS org_id VARCHAR(255);"
                    )
                except Exception as e:
                    logging.warning(f"Early org_id migration for {tbl}: {e}")
                    conn.rollback()
            conn.commit()

            # Create org_id-dependent indexes after the migration above
            org_id_indexes = [
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_prefs_with_org ON user_preferences(user_id, org_id, preference_key) WHERE org_id IS NOT NULL;",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_prefs_null_org ON user_preferences(user_id, preference_key) WHERE org_id IS NULL;",
            ]
            for index_sql in org_id_indexes:
                try:
                    cursor.execute(index_sql)
                    logging.info(f"Index created: {index_sql.split()[5]}")
                except Exception as e:
                    logging.warning(f"Error creating index: {e}")
                    conn.rollback()
            conn.commit()

            # DO NOT add k8s_clusters to RLS tables as views don't support RLS
            # Apply RLS policies to tables only
            for table_name in rls_tables:
                cursor.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;")
                logging.info(f"RLS enabled on table '{table_name}'.")
                cursor.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;")
                logging.info(f"RLS forced on table '{table_name}'.")

                # RLS condition: deny access when org_id context is not set (default-deny).
                # All code paths must SET myapp.current_org_id before querying.
                _rls_using = f"""
                    org_id IS NOT NULL
                    AND COALESCE(current_setting('myapp.current_org_id', true), '') != ''
                    AND org_id = current_setting('myapp.current_org_id', true)::text
                """

                # SELECT policy
                cursor.execute(f"""
                    DO $$ BEGIN
                        DROP POLICY IF EXISTS select_by_org ON {table_name};
                        CREATE POLICY select_by_org ON {table_name}
                        FOR SELECT USING ({_rls_using});
                    END $$;
                """)

                # Drop legacy user-based policies if they exist
                for old_policy in ['select_by_user', 'insert_by_user', 'update_by_user', 'delete_by_user']:
                    cursor.execute(f"""
                        DO $$ BEGIN
                            DROP POLICY IF EXISTS {old_policy} ON {table_name};
                        EXCEPTION WHEN undefined_object THEN NULL;
                        END $$;
                    """)

                # CRUD policies for ALL rls_tables (not just a subset)
                cursor.execute(f"""
                    DO $$ BEGIN
                        DROP POLICY IF EXISTS insert_by_org ON {table_name};
                        CREATE POLICY insert_by_org ON {table_name}
                        FOR INSERT WITH CHECK ({_rls_using});
                    END $$;
                """)
                cursor.execute(f"""
                    DO $$ BEGIN
                        DROP POLICY IF EXISTS update_by_org ON {table_name};
                        CREATE POLICY update_by_org ON {table_name}
                        FOR UPDATE USING ({_rls_using});
                    END $$;
                """)
                cursor.execute(f"""
                    DO $$ BEGIN
                        DROP POLICY IF EXISTS delete_by_org ON {table_name};
                        CREATE POLICY delete_by_org ON {table_name}
                        FOR DELETE USING ({_rls_using});
                    END $$;
                """)

                cursor.execute(
                    f"SELECT policyname, qual FROM pg_policies WHERE tablename = '{table_name}';"
                )
                policies = cursor.fetchall()
                logging.info(f"RLS policies for table '{table_name}': {policies}")

            # Commit table creation and RLS before running migrations
            conn.commit()

            # The MCP server must resolve a bearer token to (user_id, org_id) before
            # it knows which org the request belongs to. This policy permits SELECT
            # when the session explicitly opts in via myapp.mcp_token_resolve='true'.
            cursor.execute("""
                DO $$ BEGIN
                    DROP POLICY IF EXISTS select_by_token_resolve ON mcp_tokens;
                    CREATE POLICY select_by_token_resolve ON mcp_tokens
                    FOR SELECT USING (
                        current_setting('myapp.mcp_token_resolve', true) = 'true'
                    );
                END $$;
            """)
            cursor.execute("""
                DO $$ BEGIN
                    DROP POLICY IF EXISTS update_by_token_resolve ON mcp_tokens;
                    CREATE POLICY update_by_token_resolve ON mcp_tokens
                    FOR UPDATE USING (
                        current_setting('myapp.mcp_token_resolve', true) = 'true'
                    );
                END $$;
            """)
            logging.info("Added MCP token resolve RLS policies on mcp_tokens.")

            conn.commit()

            # Migration: Add role column to users table for RBAC
            try:
                cursor.execute("SAVEPOINT sp_role_col")
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(50) DEFAULT 'viewer';"
                )
                cursor.execute("RELEASE SAVEPOINT sp_role_col")
                logging.info(
                    "Added role column to users table (if not exists)."
                )
            except Exception as e:
                logging.warning(f"Error adding role column to users: {e}")
                cursor.execute("ROLLBACK TO SAVEPOINT sp_role_col")

            # Migration: Add must_change_password column to users table
            try:
                cursor.execute("SAVEPOINT sp_mcp_col")
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN DEFAULT FALSE;"
                )
                cursor.execute("RELEASE SAVEPOINT sp_mcp_col")
            except Exception as e:
                logging.warning(f"Error adding must_change_password column: {e}")
                cursor.execute("ROLLBACK TO SAVEPOINT sp_mcp_col")

            conn.commit()

            # Migration: Add org_id column to users and all org-scoped tables
            org_id_tables = [
                "users", "user_tokens", "user_connections", "user_manual_vms",
                "user_preferences", "workspaces", "aurora_deployments",
                "deployment_tasks", "deployments", "chat_sessions",
                "llm_usage_tracking", "cloud_feed_metadata", "cloud_ingestion_state",
                "grafana_alerts", "datadog_events", "netdata_alerts",
                "pagerduty_events", "opsgenie_events", "incidents", "incident_alerts",
                "rca_notification_emails", "splunk_alerts",
                "jenkins_deployment_events", "dynatrace_problems",
                "bigpanda_events", "kubectl_agent_tokens",
                "mcp_tokens",
                "k8s_pods", "k8s_nodes", "k8s_node_conditions",
                "k8s_services", "k8s_deployments", "k8s_ingresses",
                "k8s_pod_metrics", "k8s_node_metrics",
                "cloud_billing_usage", "provider_metrics",
                "knowledge_base_memory", "knowledge_base_documents",
                "incident_feedback", "postmortems",
                "incident_lifecycle_events",
                "github_connected_repos",
            ]
            for tbl in org_id_tables:
                try:
                    cursor.execute(f"SAVEPOINT sp_org_id_{tbl}")
                    cursor.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS org_id VARCHAR(255);"
                    )
                    cursor.execute(f"RELEASE SAVEPOINT sp_org_id_{tbl}")
                except Exception as e:
                    logging.warning(f"Error adding org_id to {tbl}: {e}")
                    cursor.execute(f"ROLLBACK TO SAVEPOINT sp_org_id_{tbl}")

            # Migration: Add org_id to incident child tables (linked via incident_id, not user_id)
            # Uses the same discovery query defined in org_backfill.py — single source of truth.
            from utils.db.org_backfill import _INCIDENT_CHILD_TABLES_SQL
            try:
                cursor.execute("SAVEPOINT sp_discover_child")
                cursor.execute(_INCIDENT_CHILD_TABLES_SQL)
                incident_child_tables = [row[0] for row in cursor.fetchall()]
                cursor.execute("RELEASE SAVEPOINT sp_discover_child")
            except Exception as e:
                logging.warning(f"Error discovering incident child tables: {e}")
                try:
                    cursor.execute("ROLLBACK TO SAVEPOINT sp_discover_child")
                except Exception:
                    logging.debug("ROLLBACK TO SAVEPOINT also failed for sp_discover_child")
                incident_child_tables = ["incident_thoughts", "incident_citations", "incident_suggestions"]
            for tbl in incident_child_tables:
                try:
                    cursor.execute(f"SAVEPOINT sp_org_id_{tbl}")
                    cursor.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS org_id VARCHAR(255);"
                    )
                    cursor.execute(f"RELEASE SAVEPOINT sp_org_id_{tbl}")
                except Exception as e:
                    logging.warning(f"Error adding org_id to {tbl}: {e}")
                    cursor.execute(f"ROLLBACK TO SAVEPOINT sp_org_id_{tbl}")

            # Add execution-tracking columns to incident_citations
            for col_def in [
                "duration_ms INTEGER",
                "status VARCHAR(20) DEFAULT 'success'",
                "error_message TEXT",
            ]:
                col_name = col_def.split()[0]
                try:
                    cursor.execute(f"SAVEPOINT sp_cit_{col_name}")
                    cursor.execute(
                        f"ALTER TABLE incident_citations ADD COLUMN IF NOT EXISTS {col_def};"
                    )
                    cursor.execute(f"RELEASE SAVEPOINT sp_cit_{col_name}")
                except Exception as e:
                    logging.warning(f"Error adding {col_name} to incident_citations: {e}")
                    cursor.execute(f"ROLLBACK TO SAVEPOINT sp_cit_{col_name}")

            # Add tool_call_id to execution_steps for precise citation matching
            try:
                cursor.execute("SAVEPOINT sp_es_tcid")
                cursor.execute(
                    "ALTER TABLE execution_steps ADD COLUMN IF NOT EXISTS tool_call_id VARCHAR(255);"
                )
                cursor.execute("RELEASE SAVEPOINT sp_es_tcid")
            except Exception as e:
                logging.warning(f"Error adding tool_call_id to execution_steps: {e}")
                cursor.execute("ROLLBACK TO SAVEPOINT sp_es_tcid")

            # DB triggers: auto-inherit org_id from parent incident on INSERT.
            # This means no INSERT statement in the codebase ever needs to set
            # org_id on these tables — the trigger handles it unconditionally.
            for tbl in incident_child_tables:
                fn_name = f"fn_{tbl}_inherit_org_id"
                trg_name = f"trg_{tbl}_inherit_org_id"
                try:
                    cursor.execute(f"SAVEPOINT sp_trg_{tbl}")
                    cursor.execute(f"""
                        CREATE OR REPLACE FUNCTION {fn_name}()
                        RETURNS TRIGGER AS $trg$
                        BEGIN
                            NEW.org_id := (
                                SELECT org_id FROM incidents WHERE id = NEW.incident_id
                            );
                            RETURN NEW;
                        END;
                        $trg$ LANGUAGE plpgsql;

                        DROP TRIGGER IF EXISTS {trg_name} ON {tbl};
                        CREATE TRIGGER {trg_name}
                            BEFORE INSERT ON {tbl}
                            FOR EACH ROW
                            EXECUTE FUNCTION {fn_name}();
                    """)
                    cursor.execute(f"RELEASE SAVEPOINT sp_trg_{tbl}")
                except Exception as e:
                    logging.warning(f"Error creating org_id trigger for {tbl}: {e}")
                    try:
                        cursor.execute(f"ROLLBACK TO SAVEPOINT sp_trg_{tbl}")
                    except Exception:
                        logging.debug(f"ROLLBACK TO SAVEPOINT also failed for sp_trg_{tbl}")

            # Add FK from users.org_id -> organizations.id (if not exists)
            try:
                cursor.execute("SAVEPOINT sp_org_fk")
                cursor.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'users_org_id_fkey'
                        ) THEN
                            ALTER TABLE users
                            ADD CONSTRAINT users_org_id_fkey
                            FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE SET NULL;
                        END IF;
                    END $$;
                """)
                cursor.execute("RELEASE SAVEPOINT sp_org_fk")
            except Exception as e:
                logging.warning(f"Error adding users.org_id FK: {e}")
                cursor.execute("ROLLBACK TO SAVEPOINT sp_org_fk")

            # Backfill org_id on all data tables — single source of truth
            # in utils/db/org_backfill.py.  Covers user-scoped tables (dynamic
            # discovery) AND incident child tables (via incident_id FK).
            try:
                from utils.db.org_backfill import backfill_all_users_at_boot
                backfill_all_users_at_boot(cursor)
                conn.commit()
            except Exception as e:
                logging.warning(f"Error in org_id boot backfill: {e}")
                conn.rollback()

            # Repair: ensure every user with an org has the correct Casbin role.
            # Self-healing: org creators are promoted to admin even if role was viewer.
            try:
                from utils.auth.enforcer import assign_role_to_user, get_user_roles_in_org
                cursor.execute("""
                    SELECT u.id, u.role, u.org_id,
                           CASE WHEN o.created_by = u.id THEN TRUE ELSE FALSE END AS is_creator
                    FROM users u
                    LEFT JOIN organizations o ON u.org_id = o.id
                    WHERE u.org_id IS NOT NULL
                """)
                for uid, urole, uorg, is_creator in cursor.fetchall():
                    expected_role = "admin" if is_creator else (urole or "viewer")
                    current_casbin = get_user_roles_in_org(uid, uorg)
                    if not current_casbin or current_casbin != [expected_role]:
                        try:
                            if is_creator and urole != "admin":
                                cursor.execute("SAVEPOINT sp_role_repair")
                                cursor.execute(
                                    "UPDATE users SET role = 'admin' WHERE id = %s",
                                    (uid,),
                                )
                                cursor.execute("RELEASE SAVEPOINT sp_role_repair")
                            assign_role_to_user(uid, expected_role, uorg)
                            logging.info(
                                "Repaired Casbin role for user %s: %s -> %s (creator=%s)",
                                uid, current_casbin, expected_role, is_creator,
                            )
                        except Exception as casbin_err:
                            try:
                                cursor.execute("ROLLBACK TO SAVEPOINT sp_role_repair")
                            except Exception as rollback_err:
                                logging.warning(
                                    "Failed to roll back savepoint for user %s during role repair: %s",
                                    uid, rollback_err,
                                )
                            logging.warning(
                                "Failed to repair Casbin role for user %s: %s",
                                uid, casbin_err,
                            )
                conn.commit()
            except Exception as e:
                logging.warning(f"Error in Casbin role repair: {e}")
                conn.rollback()

            # Create org_id indexes for performance
            for tbl in org_id_tables:
                try:
                    cursor.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{tbl}_org_id ON {tbl}(org_id);"
                    )
                except Exception as e:
                    logging.warning(f"Error creating org_id index for {tbl}: {e}")

            # Migration: update UNIQUE constraints to include org_id
            # incidents: (source_type, source_alert_id, user_id) -> (org_id, source_type, source_alert_id, user_id)
            try:
                cursor.execute("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'incidents_source_type_source_alert_id_user_id_key'
                        ) THEN
                            ALTER TABLE incidents DROP CONSTRAINT incidents_source_type_source_alert_id_user_id_key;
                            ALTER TABLE incidents ADD CONSTRAINT incidents_org_source_alert_user_key
                                UNIQUE(org_id, source_type, source_alert_id, user_id);
                        END IF;
                    END $$;
                """)
            except Exception as e:
                logging.warning(f"Error migrating incidents UNIQUE constraint: {e}")
                conn.rollback()

            # knowledge_base_memory: UNIQUE(user_id) -> UNIQUE(user_id, org_id)
            try:
                cursor.execute("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'knowledge_base_memory_user_id_key'
                        ) THEN
                            ALTER TABLE knowledge_base_memory DROP CONSTRAINT knowledge_base_memory_user_id_key;
                            ALTER TABLE knowledge_base_memory ADD CONSTRAINT knowledge_base_memory_user_org_key
                                UNIQUE(user_id, org_id);
                        END IF;
                    END $$;
                """)
            except Exception as e:
                logging.warning(f"Error migrating knowledge_base_memory UNIQUE constraint: {e}")
                conn.rollback()

            # user_preferences: UNIQUE(user_id, preference_key) -> UNIQUE(user_id, org_id, preference_key)
            try:
                cursor.execute("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'user_preferences_user_id_preference_key_key'
                        ) THEN
                            ALTER TABLE user_preferences DROP CONSTRAINT user_preferences_user_id_preference_key_key;
                            ALTER TABLE user_preferences ADD CONSTRAINT user_preferences_user_org_pref_key
                                UNIQUE(user_id, org_id, preference_key);
                        END IF;
                    END $$;
                """)
            except Exception as e:
                logging.warning(f"Error migrating user_preferences UNIQUE constraint: {e}")
                conn.rollback()

            # rca_notification_emails: add UNIQUE(org_id, email) for org-scoped deduplication
            try:
                cursor.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'rca_notification_emails_org_id_email_key'
                        ) THEN
                            ALTER TABLE rca_notification_emails ADD CONSTRAINT rca_notification_emails_org_id_email_key
                                UNIQUE(org_id, email);
                        END IF;
                    END $$;
                """)
            except Exception as e:
                logging.warning(f"Error adding rca_notification_emails org_email UNIQUE constraint: {e}")
                conn.rollback()

            # org_invitations: add expires_at column if missing
            try:
                cursor.execute("""
                    ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS
                        expires_at TIMESTAMP DEFAULT (NOW() + INTERVAL '7 days');
                """)
            except Exception as e:
                logging.warning(f"Error adding expires_at to org_invitations: {e}")
                conn.rollback()

            # Create k8s_clusters view (after org_id migration so the column exists)
            # DROP first because CREATE OR REPLACE VIEW cannot remove columns from an existing view
            try:
                cursor.execute("DROP VIEW IF EXISTS k8s_clusters;")
                create_clusters_view_sql = """
                    CREATE VIEW k8s_clusters AS
                    SELECT DISTINCT project_id, cluster_name, provider, user_id, org_id
                    FROM k8s_nodes
                    WHERE org_id = current_setting('myapp.current_org_id', true)::text;
                """
                cursor.execute(create_clusters_view_sql)
                logging.info("View 'k8s_clusters' created successfully.")
            except Exception as e:
                logging.warning(f"Error creating k8s_clusters view: {e}")
                conn.rollback()

            # Migration: De-duplicate organization names from before uniqueness was enforced.
            # Appends a short ID suffix to the newer duplicate(s) so names are unique going forward.
            try:
                cursor.execute("""
                    UPDATE organizations SET name = name || ' (' || LEFT(id, 8) || ')'
                    WHERE id IN (
                        SELECT id FROM (
                            SELECT id,
                                   ROW_NUMBER() OVER (PARTITION BY LOWER(name) ORDER BY created_at ASC) AS rn
                            FROM organizations
                        ) dupes WHERE rn > 1
                    );
                """)
                if cursor.rowcount > 0:
                    logging.info(f"De-duplicated {cursor.rowcount} organization name(s).")
            except Exception as e:
                logging.warning(f"Error de-duplicating organization names: {e}")
                conn.rollback()

            conn.commit()
            logging.info("Database tables initialized successfully.")
            cursor.close()

            # Release advisory lock
            try:
                with conn.cursor() as unlock_cursor:  # No RLS needed — advisory lock release
                    unlock_cursor.execute("SELECT pg_advisory_unlock(1234567890);")
            except Exception as unlock_error:
                logging.warning(f"Error releasing advisory lock: {unlock_error}")

    except Exception as e:
        logging.error(f"Error initializing tables: {e}")
        raise


def store_data_in_db(data):
    """
    Stores billing data into PostgreSQL database using connection pool.
    :param data: List of dictionaries with billing data.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()  # No RLS needed — cloud_billing_usage not RLS-protected

            insert_query = """
                INSERT INTO cloud_billing_usage (
                    service, sku, category, cost, usage, unit, usage_date,
                    region, project_id, currency, dataset_id, table_name,
                    user_id, provider
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """

            psycopg2.extras.execute_batch(
                cursor,
                insert_query,
                [
                    (
                        row.get("service"),
                        row.get("sku"),
                        row.get("category"),
                        row.get("cost"),
                        row.get("usage"),
                        row.get("unit"),
                        row.get("usage_date"),
                        row.get("region"),
                        row.get("project_id"),
                        row.get("currency"),
                        row.get("dataset_id"),
                        row.get("table_name"),
                        row.get("user_id"),
                        row.get("provider"),
                    )
                    for row in data
                ],
            )

            conn.commit()
            print("Data successfully inserted into the database.")
            cursor.close()

    except DatabaseError as e:
        print(f"Database error during data insertion: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")


if __name__ == "__main__":
    ensure_database_exists()
    initialize_tables()
