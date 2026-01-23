"""Cleanup utility for idle terminal pods.

Tracks LAST ACTIVITY time (not creation) to avoid deleting active pods.
Deletes pods idle for longer than their TTL with 5-min grace period.
"""

import logging
import time
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from celery_config import celery_app

logger = logging.getLogger(__name__)


class TerminalPodCleaner:
    """Cleans up idle terminal pods based on inactivity TTL."""
    
    def __init__(self, namespace: str = "untrusted"):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.core_v1 = client.CoreV1Api()
        self.namespace = namespace
    
    def cleanup_idle_pods(self, dry_run: bool = False, min_age_seconds: int = 300) -> dict:
        """Delete pods idle > TTL with min_age_seconds grace period."""
        stats = {'scanned': 0, 'deleted': 0, 'kept': 0}
        current_time = int(time.time())
        
        try:
            pods = self.core_v1.list_namespaced_pod(
                namespace=self.namespace, label_selector="app=user-terminal"
            )
            stats['scanned'] = len(pods.items)
            
            for pod in pods.items:
                try:
                    name = pod.metadata.name
                    phase = pod.status.phase
                    
                    # Delete failed/succeeded pods immediately
                    if phase in ['Failed', 'Succeeded', 'Unknown']:
                        if not dry_run:
                            self.core_v1.delete_namespaced_pod(name, self.namespace, grace_period_seconds=30)
                        logger.info(f"{'[DRY-RUN] ' if dry_run else ''}Deleted {name} (phase: {phase})")
                        stats['deleted'] += 1
                        continue
                    
                    # Get timestamps and TTL
                    created_at = int(pod.metadata.labels.get('created-at', '0'))
                    last_activity = pod.metadata.annotations.get('last-activity-at')
                    ttl = int(pod.metadata.annotations.get('inactivity-timeout-seconds', '600'))
                    
                    pod_age = current_time - created_at
                    idle_time = current_time - int(last_activity) if last_activity else pod_age
                    
                    # Grace period: skip pods younger than min_age_seconds
                    if pod_age < min_age_seconds:
                        stats['kept'] += 1
                        continue
                    
                    # Delete if idle > TTL
                    if idle_time > ttl:
                        if not dry_run:
                            self.core_v1.delete_namespaced_pod(name, self.namespace, grace_period_seconds=30)
                        logger.info(f"{'[DRY-RUN] ' if dry_run else ''}Deleted {name} (idle: {idle_time}s > {ttl}s)")
                        stats['deleted'] += 1
                    else:
                        stats['kept'] += 1
                
                except (ValueError, ApiException) as e:
                    logger.warning(f"Failed to process pod: {e}")
                    continue  # Keep going, don't let one failure stop cleanup
                    
        except ApiException as e:
            logger.error(f"Failed to list pods: {e}")
        
        logger.info(f"Cleanup: scanned={stats['scanned']}, deleted={stats['deleted']}, kept={stats['kept']}")
        return stats


@celery_app.task(name='utils.terminal.terminal_pod_cleanup.cleanup_terminal_pods_task')
def cleanup_terminal_pods_task():
    """Celery periodic task for cleaning up idle terminal pods."""
    import os
    
    # Only run if pod isolation is enabled
    if os.getenv('ENABLE_POD_ISOLATION', 'false').lower() != 'true':
        logger.debug("Pod isolation disabled, skipping terminal pod cleanup")
        return {'skipped': True, 'reason': 'pod_isolation_disabled'}
    
    # Check if we can connect to Kubernetes
    try:
        logger.info("Starting scheduled terminal pod cleanup")
        stats = TerminalPodCleaner().cleanup_idle_pods(dry_run=False, min_age_seconds=300)
        logger.info(f"Scheduled cleanup completed: {stats}")
        return stats
    except config.ConfigException as e:
        logger.debug(f"Kubernetes not available, skipping cleanup: {e}")
        return {'skipped': True, 'reason': 'kubernetes_not_available'}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    result = TerminalPodCleaner().cleanup_idle_pods(dry_run="--dry-run" in sys.argv, min_age_seconds=300)
    print(f"Result: {result}")

