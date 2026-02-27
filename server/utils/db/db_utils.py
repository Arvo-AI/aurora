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


def ensure_database_exists():
    """Ensure that the target database exists.
    Note: With unified postgres credentials, the database is typically created by the postgres container.
    This function verifies connectivity and creates the database if needed."""
    logging.debug("Starting the database existence check.")
    conn = None
    cursor = None
    try:
        # First connect to 'postgres' database to check/create target database
        init_params = DB_PARAMS.copy()
        init_params["dbname"] = "postgres"

        logging.debug(f"Connecting to postgres database as {init_params['user']}")
        conn = psycopg2.connect(**init_params)
        conn.autocommit = True
        cursor = conn.cursor()
        logging.info("Connected to postgres database.")

        # Create the target database if it doesn't exist
        target_db = DB_PARAMS["dbname"]
        cursor.execute(f"SELECT 1 FROM pg_database WHERE datname = '{target_db}';")
        if not cursor.fetchone():
            cursor.execute(f"CREATE DATABASE {target_db};")
            logging.info(f"Database '{target_db}' created successfully.")
        else:
            logging.info(f"Database '{target_db}' already exists.")

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
            cursor = conn.cursor()

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
                        provider VARCHAR(50),
                        UNIQUE (metric_name, timestamp, labels, resource_type, resource_labels, category, unit, user_id)
                    );
                """,
                "user_tokens": """
                    CREATE TABLE IF NOT EXISTS user_tokens (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
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
                "user_manual_vms": """
                    CREATE TABLE IF NOT EXISTS user_manual_vms (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
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
                        provider VARCHAR(50) NOT NULL,
                        account_id VARCHAR(255) NOT NULL,
                        role_arn VARCHAR(512),
                        connection_method VARCHAR(50),
                        status VARCHAR(20) DEFAULT 'active', -- active | not_connected | error
                        last_verified_at TIMESTAMP,
                        UNIQUE(user_id, provider, account_id)
                    );
                """,
                "user_preferences": """
                    CREATE TABLE IF NOT EXISTS user_preferences (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        preference_key VARCHAR(255) NOT NULL,
                        preference_value JSONB,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, preference_key)
                    );
                """,
                "workspaces": """
                    CREATE TABLE IF NOT EXISTS workspaces (
                        id VARCHAR(50) PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
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
                        task_id VARCHAR(100),
                        service_name_map JSONB
                    );
                """,
                "chat_sessions": """
                    CREATE TABLE IF NOT EXISTS chat_sessions (
                        id VARCHAR(50) PRIMARY KEY,
                        user_id VARCHAR(1000) NOT NULL,
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
                        session_id VARCHAR(50),
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        model_name VARCHAR(255) NOT NULL,
                        api_provider VARCHAR(100) DEFAULT 'openrouter',
                        request_type VARCHAR(100),
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER GENERATED ALWAYS AS (input_tokens + output_tokens) STORED,
                        estimated_cost DECIMAL(10,6) DEFAULT 0.00,
                        surcharge_rate DECIMAL(5,4) DEFAULT 0.3000,
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
                "netdata_alerts": """
                    CREATE TABLE IF NOT EXISTS netdata_verification_tokens (
                        user_id TEXT PRIMARY KEY,
                        token TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS netdata_alerts (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
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
                "incidents": """
                     CREATE TABLE IF NOT EXISTS incidents (
                         id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                         user_id VARCHAR(255) NOT NULL,
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
                         active_tab VARCHAR(10) DEFAULT 'thoughts',
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         merged_into_incident_id UUID REFERENCES incidents(id) ON DELETE SET NULL,
                         UNIQUE(source_type, source_alert_id, user_id)
                     );
                     
                     CREATE INDEX IF NOT EXISTS idx_incidents_user_id ON incidents(user_id, started_at DESC);
                     CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
                     CREATE INDEX IF NOT EXISTS idx_incidents_source ON incidents(source_type, source_alert_id);
                     CREATE INDEX IF NOT EXISTS idx_incidents_merged ON incidents(merged_into_incident_id) WHERE merged_into_incident_id IS NOT NULL;
                 """,
                "incident_alerts": """
                    CREATE TABLE IF NOT EXISTS incident_alerts (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
                        source_type VARCHAR(20) NOT NULL,
                        source_alert_id INTEGER NOT NULL,
                        alert_title TEXT,
                        alert_service TEXT,
                        alert_environment TEXT,
                        aurora_status VARCHAR(20) DEFAULT 'idle',
                        aurora_summary TEXT,
                        aurora_chat_session_id UUID,
                        rca_celery_task_id VARCHAR(255),
                        started_at TIMESTAMP NOT NULL,
                        analyzed_at TIMESTAMP,
                        slack_message_ts VARCHAR(50),
                        active_tab VARCHAR(10) DEFAULT 'thoughts',
                        visualization_code TEXT,
                        visualization_updated_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(source_type, source_alert_id, user_id)
                        alert_severity VARCHAR(20),
                        correlation_strategy TEXT,
                        correlation_score FLOAT,
                        correlation_details JSONB,
                        alert_metadata JSONB,
                        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_incident_alerts_incident_id ON incident_alerts(incident_id);
                    CREATE INDEX IF NOT EXISTS idx_incident_alerts_source ON incident_alerts(source_type, source_alert_id);
                """,
                "incident_alerts": """
                    CREATE TABLE IF NOT EXISTS incident_alerts (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id VARCHAR(1000) NOT NULL,
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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(incident_id, citation_key)
                    );

                    CREATE INDEX IF NOT EXISTS idx_incident_citations_incident_id ON incident_citations(incident_id);
                """,
                "rca_notification_emails": """
                    CREATE TABLE IF NOT EXISTS rca_notification_emails (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(1000) NOT NULL,
                        email VARCHAR(255) NOT NULL,
                        is_verified BOOLEAN DEFAULT FALSE,
                        is_enabled BOOLEAN DEFAULT TRUE,
                        verification_code VARCHAR(6),
                        verification_code_expires_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        verified_at TIMESTAMP,
                        UNIQUE(user_id, email)
                    );

                    CREATE INDEX IF NOT EXISTS idx_rca_emails_user_id ON rca_notification_emails(user_id);
                    CREATE INDEX IF NOT EXISTS idx_rca_emails_verified ON rca_notification_emails(user_id, is_verified);
                    CREATE INDEX IF NOT EXISTS idx_rca_emails_enabled ON rca_notification_emails(user_id, is_verified, is_enabled);
                """,
                "splunk_alerts": """
                    CREATE TABLE IF NOT EXISTS splunk_alerts (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
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
                "jenkins_deployment_events": """
                    CREATE TABLE IF NOT EXISTS jenkins_deployment_events (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_jenkins_deploy_user ON jenkins_deployment_events(user_id, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_jenkins_deploy_service ON jenkins_deployment_events(service, received_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_jenkins_deploy_commit ON jenkins_deployment_events(commit_sha);
                    CREATE INDEX IF NOT EXISTS idx_jenkins_deploy_trace ON jenkins_deployment_events(trace_id) WHERE trace_id IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_jenkins_deploy_dedup ON jenkins_deployment_events(user_id, COALESCE(job_name, ''), COALESCE(build_number, -1));
                """,
                "dynatrace_problems": """
                    CREATE TABLE IF NOT EXISTS dynatrace_problems (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
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
                "kubectl_agent_tokens": """
                    CREATE TABLE IF NOT EXISTS kubectl_agent_tokens (
                        id SERIAL PRIMARY KEY,
                        token VARCHAR(128) UNIQUE NOT NULL,
                        user_id TEXT NOT NULL,
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
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    );
                    
                    CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
                """,
                "knowledge_base_memory": """
                    CREATE TABLE IF NOT EXISTS knowledge_base_memory (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(1000) NOT NULL UNIQUE,
                        content TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_kb_memory_user_id ON knowledge_base_memory(user_id);
                """,
                "knowledge_base_documents": """
                    CREATE TABLE IF NOT EXISTS knowledge_base_documents (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id VARCHAR(1000) NOT NULL,
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
                        content TEXT,
                        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        confluence_page_id TEXT,
                        confluence_page_url TEXT,
                        confluence_exported_at TIMESTAMP
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_postmortems_incident_id ON postmortems(incident_id);
                    CREATE UNIQUE INDEX IF NOT EXISTS postmortems_incident_id_unique ON postmortems(incident_id);
                    CREATE INDEX IF NOT EXISTS idx_postmortems_user_id ON postmortems(user_id);
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
                "user_manual_vms",
            ]

            # Add monitoring tables
            rls_tables.append("grafana_alerts")
            rls_tables.append("datadog_events")
            rls_tables.append("netdata_alerts")
            rls_tables.append("netdata_verification_tokens")
            rls_tables.append("splunk_alerts")
            rls_tables.append("jenkins_deployment_events")
            rls_tables.append("dynatrace_problems")

            # Add incidents table
            # Note: incident_suggestions and incident_thoughts are child tables with CASCADE DELETE
            # so they don't need RLS - incident_alerts is protected separately for safety
            rls_tables.append("incidents")
            rls_tables.append("incident_alerts")
            rls_tables.append("incident_feedback")
            rls_tables.append("postmortems")


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

            # Migration: Add surcharge fields to llm_usage_tracking table if they don't exist
            try:
                cursor.execute("""
                    ALTER TABLE llm_usage_tracking 
                    ADD COLUMN IF NOT EXISTS surcharge_rate DECIMAL(5,4) DEFAULT 0.3000;
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
                        confluence_exported_at TIMESTAMP
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

            # Create a view for clusters after all tables are created
            create_clusters_view_sql = """
                CREATE OR REPLACE VIEW k8s_clusters AS
                SELECT DISTINCT project_id, cluster_name, provider, user_id
                FROM k8s_nodes
                WHERE user_id = current_setting('myapp.current_user_id', true)::text;
            """
            cursor.execute(create_clusters_view_sql)
            logging.info("View 'k8s_clusters' created successfully.")

            # DO NOT add k8s_clusters to RLS tables as views don't support RLS
            # Apply RLS policies to tables only
            for table_name in rls_tables:
                cursor.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;")
                logging.info(f"RLS enabled on table '{table_name}'.")
                cursor.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;")
                logging.info(f"RLS forced on table '{table_name}'.")

                # Create SELECT policy
                policy_sql = f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_policies
                            WHERE tablename = '{table_name}'
                            AND policyname = 'select_by_user'
                        ) THEN
                            IF '{table_name}' = 'incidents' THEN
                                -- For incidents table, allow selecting own incidents OR demo incidents
                                CREATE POLICY select_by_user ON {table_name}
                                FOR SELECT USING (
                                    user_id = current_setting('myapp.current_user_id')::text
                                    OR (alert_metadata::jsonb->>'is_demo')::boolean = true
                                );
                            ELSE
                                -- For other tables, standard user-based filtering
                                CREATE POLICY select_by_user ON {table_name}
                                FOR SELECT USING (user_id = current_setting('myapp.current_user_id')::text);
                            END IF;
                        END IF;
                    END $$;
                    """
                cursor.execute(policy_sql)
                logging.info(
                    f"RLS SELECT policy creation command executed for table '{table_name}'."
                )

                # For tables that need full CRUD policies, create INSERT, UPDATE, and DELETE policies
                if table_name in [
                    "chat_sessions",
                    "user_preferences",
                    "deployment_tasks",
                    "user_tokens",
                    "llm_usage_tracking",
                    "kubectl_agent_tokens",
                    "user_manual_vms",
                    "incident_alerts",
                    "incident_feedback",
                    "postmortems",
                ]:
                    # INSERT policy
                    insert_policy_sql = f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_policies
                                WHERE tablename = '{table_name}'
                                AND policyname = 'insert_by_user'
                            ) THEN
                                CREATE POLICY insert_by_user ON {table_name}
                                FOR INSERT WITH CHECK (user_id = current_setting('myapp.current_user_id')::text);
                            END IF;
                        END $$;
                        """
                    cursor.execute(insert_policy_sql)
                    logging.info(
                        f"RLS INSERT policy creation command executed for table '{table_name}'."
                    )

                    # UPDATE policy
                    update_policy_sql = f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_policies
                                WHERE tablename = '{table_name}'
                                AND policyname = 'update_by_user'
                            ) THEN
                                CREATE POLICY update_by_user ON {table_name}
                                FOR UPDATE USING (user_id = current_setting('myapp.current_user_id')::text);
                            END IF;
                        END $$;
                        """
                    cursor.execute(update_policy_sql)
                    logging.info(
                        f"RLS UPDATE policy creation command executed for table '{table_name}'."
                    )

                    # DELETE policy
                    delete_policy_sql = f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_policies
                                WHERE tablename = '{table_name}'
                                AND policyname = 'delete_by_user'
                            ) THEN
                                CREATE POLICY delete_by_user ON {table_name}
                                FOR DELETE USING (user_id = current_setting('myapp.current_user_id')::text);
                            END IF;
                        END $$;
                        """
                    cursor.execute(delete_policy_sql)
                    logging.info(
                        f"RLS DELETE policy creation command executed for table '{table_name}'."
                    )

                cursor.execute(
                    f"SELECT policyname, qual FROM pg_policies WHERE tablename = '{table_name}';"
                )
                policies = cursor.fetchall()
                logging.info(f"RLS policies for table '{table_name}': {policies}")

            conn.commit()
            logging.info("Database tables initialized successfully.")
            cursor.close()

            # Release advisory lock
            try:
                with conn.cursor() as unlock_cursor:
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
            cursor = conn.cursor()

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
