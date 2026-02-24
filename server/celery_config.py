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
celery_app = Celery('aurora_tasks',
                    broker=os.getenv('REDIS_URL', 'redis://redis:6379/0'),
                    backend=os.getenv('REDIS_URL', 'redis://redis:6379/0'))

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
    # Explicitly include task modules from their new locations
    include=[
        'connectors.gcp_connector.gcp_post_auth_tasks',
        'routes.gcp.root_project_tasks',
        'routes.grafana.tasks',
        'routes.datadog.tasks',
        'routes.netdata.tasks',
        'routes.splunk.tasks',
        'routes.pagerduty.tasks',
        'utils.terminal.terminal_pod_cleanup',
        'chat.background.task',
        'chat.background.summarization',
        'chat.background.visualization_generator',
        'chat.background.postmortem_generator',
        'routes.knowledge_base.tasks',
        'services.discovery.tasks',
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
    from chat.background import postmortem_generator  # noqa: F401
    logging.info("Background chat tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import background chat tasks: {e}")

try:
    import routes.pagerduty.tasks
    logging.info("PagerDuty tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import PagerDuty tasks: {e}")

try:
    import services.discovery.tasks
    logging.info("Discovery tasks imported successfully")
except ImportError as e:
    logging.warning(f"Failed to import discovery tasks: {e}")

# Log the number of registered tasks for debugging
if hasattr(celery_app, 'tasks'):
    non_celery_tasks = [t for t in celery_app.tasks.keys() if not t.startswith('celery.')]
    logging.info("Registered %d custom tasks: %s", len(non_celery_tasks), non_celery_tasks)
