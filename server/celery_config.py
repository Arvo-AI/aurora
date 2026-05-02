from celery import Celery
import os
import logging
from dotenv import load_dotenv

# ------------------------------------------------------------
# Configure root logger BEFORE Celery starts.
# Uses stdout-only logging for container-native log aggregation.
# Logs are accessible via `docker logs` or `kubectl logs`.
# ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
    force=True  # Remove any existing handlers set by other modules to avoid duplicate logs
)

# Prevent Celery from replacing the root logger handlers when the worker
# starts. This MUST be set before the worker process initialises logging.
os.environ.setdefault("CELERYD_HIJACK_ROOT_LOGGER", "False")

# ------------------------------------------------------------

# Load environment variables
load_dotenv()

# Initialize Celery
redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')
celery_app = Celery('aurora_tasks',
                    broker=redis_url,
                    backend=redis_url)

# Configure SSL for Redis broker/backend when using rediss:// scheme
if redis_url.startswith('rediss://'):
    import ssl as _ssl
    _ssl_cert_reqs_map = {
        'none': _ssl.CERT_NONE,
        'optional': _ssl.CERT_OPTIONAL,
        'required': _ssl.CERT_REQUIRED,
    }
    _ssl_cert_reqs_str = os.getenv('REDIS_SSL_CERT_REQS', '').strip().lower()
    if not _ssl_cert_reqs_str:
        _ssl_cert_reqs_str = 'required'
    if _ssl_cert_reqs_str not in _ssl_cert_reqs_map:
        raise ValueError(f"Invalid REDIS_SSL_CERT_REQS={_ssl_cert_reqs_str!r}, must be one of: {', '.join(_ssl_cert_reqs_map)}")
    _broker_ssl = {
        'ssl_cert_reqs': _ssl_cert_reqs_map[_ssl_cert_reqs_str],
    }
    _ssl_ca_certs = os.getenv('REDIS_SSL_CA_CERTS')
    if _ssl_ca_certs:
        _broker_ssl['ssl_ca_certs'] = _ssl_ca_certs
    celery_app.conf.update(
        broker_use_ssl=_broker_ssl,
        redis_backend_use_ssl=_broker_ssl,
    )

# Configure Celery
celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=(60*60*3),  # 3 hour timeout
    worker_max_tasks_per_child=1,  # Restart worker after each task
    worker_prefetch_multiplier=1,  # Process one task at a time
    broker_connection_retry_on_startup=True,  # Explicitly enable for Celery 6.0+
    result_expires=3600,  # Expire task results after 1 hour (backend= is set above)
    # Explicitly include task modules from their new locations
    include=[
        'connectors.gcp_connector.gcp_post_auth_tasks',
        'routes.gcp.root_project_tasks',
        'routes.grafana.tasks',
        'routes.datadog.tasks',
        'routes.netdata.tasks',
        'routes.splunk.tasks',
        'routes.dynatrace.tasks',
        'routes.bigpanda.tasks',
        'routes.pagerduty.tasks',
        'routes.opsgenie.tasks',
        'routes.newrelic.tasks',
        'routes.jenkins.tasks',
        'routes.spinnaker.tasks',
        'routes.incidentio.tasks',
        'utils.terminal.terminal_pod_cleanup',
        'chat.background.task',
        'chat.background.summarization',
        'chat.background.visualization_generator',
        'chat.background.postmortem_generator',
        'chat.background.prediscovery_task',
        'routes.knowledge_base.tasks',
        'services.discovery.tasks',
        'utils.aws.credential_refresh',
        'routes.github.github_repo_metadata',
        'routes.change_intercept.tasks',
    ],
    # Periodic task schedule
    beat_schedule={
        'cleanup-idle-terminal-pods': {
            'task': 'utils.terminal.terminal_pod_cleanup.cleanup_terminal_pods_task',
            'schedule': 600.0,  # Every 10 minutes
        },
        'cleanup-stale-background-chats': {
            'task': 'chat.background.cleanup_stale_sessions',
            'schedule': 300.0,  # Every 5 minutes
        },
        'cleanup-stale-kb-documents': {
            'task': 'knowledge_base.cleanup_stale_documents',
            'schedule': 180.0,  # Every 3 minutes
        },
        'run-full-discovery': {
            'task': 'services.discovery.tasks.run_full_discovery',
            'schedule': float(os.getenv('DISCOVERY_INTERVAL_HOURS', '1')) * 3600,  # Default: every hour
        },
        'mark-stale-services': {
            'task': 'services.discovery.tasks.mark_stale_services',
            'schedule': 86400.0,  # Daily (24 hours)
        },
        'run-prediscovery': {
            'task': 'chat.background.prediscovery_task.run_prediscovery_all_orgs',
            'schedule': 3600.0,  # Check hourly; per-org interval controlled by prediscovery_interval_hours preference
        },
        'refresh-aws-credentials': {
            'task': 'utils.aws.credential_refresh.refresh_aws_credentials',
            'schedule': 600.0,  # Every 10 minutes
        },
    },
    beat_schedule_filename='celerybeat-schedule',
    worker_hijack_root_logger=False
) 

# Manually import task modules to ensure they're registered
# This is crucial after moving the files to new locations
try:
    import connectors.gcp_connector.gcp_post_auth_tasks
    import routes.gcp.root_project_tasks
    logging.info("GCP task modules imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import GCP task modules: {e}")

try:
    import chat.background.task
    import chat.background.summarization
    import chat.background.visualization_generator
    import chat.background.postmortem_generator
    logging.info("Background chat tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import background chat tasks: {e}")

try:
    import routes.dynatrace.tasks  # noqa: F401
    logging.info("Dynatrace tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import Dynatrace tasks: {e}")

try:
    import routes.bigpanda.tasks  # noqa: F401
    logging.info("BigPanda tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import BigPanda tasks: {e}")

try:
    import routes.pagerduty.tasks
    logging.info("PagerDuty tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import PagerDuty tasks: {e}")

try:
    import routes.opsgenie.tasks  # noqa: F401
    logging.info("OpsGenie tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import OpsGenie tasks: {e}")

try:
    import routes.jenkins.tasks  # noqa: F401
    logging.info("Jenkins tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import Jenkins tasks: {e}")

try:
    import routes.spinnaker.tasks  # noqa: F401
    logging.info("Spinnaker tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import Spinnaker tasks: {e}")

try:
    import services.discovery.tasks
    logging.info("Discovery tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import discovery tasks: {e}")

try:
    import chat.background.prediscovery_task  # noqa: F401
    logging.info("Prediscovery task imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import prediscovery task: {e}")

try:
    import utils.aws.credential_refresh
    logging.info("AWS credential refresh task imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import AWS credential refresh task: {e}")

try:
    import routes.newrelic.tasks  # noqa: F401
    logging.info("New Relic tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import New Relic tasks: {e}")

try:
    import routes.github.github_repo_metadata  # noqa: F401
    logging.info("GitHub repo metadata task imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import GitHub repo metadata task: {e}")

try:
    import routes.change_intercept.tasks  # noqa: F401
    logging.info("Change-intercept tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import change-intercept tasks: {e}")

# Log the number of registered tasks for debugging
if hasattr(celery_app, 'tasks'):
    non_celery_tasks = [t for t in celery_app.tasks.keys() if not t.startswith('celery.')]
    logging.info("Registered %d custom tasks: %s", len(non_celery_tasks), non_celery_tasks)
