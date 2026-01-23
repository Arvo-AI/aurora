# service_usage_utils.py

import logging
from googleapiclient import discovery
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from typing import List
import time

logger = logging.getLogger(__name__)

# Suppress googleapiclient discovery cache warnings
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

class ServiceUsageManager:
    def __init__(self, project_id: str, credentials: Credentials):
        self.project_id = project_id
        self.credentials = credentials
        self.service_client = discovery.build('serviceusage', 'v1', credentials=credentials, cache_discovery=False)

    def ensure_services_enabled(self, required_services: List[str]):
        for service in required_services:
            try:
                if not self.is_service_enabled(service):
                    self.enable_service(service)
                else:
                    logger.info(f"Service '{service}' is already enabled.")
            except HttpError as e:
                logger.error(f"Could not enable service {service}: {e}")

    def is_service_enabled(self, service_name: str) -> bool:
        """
        Checks if a specific GCP service is enabled for the project.
        """
        try:
            request = self.service_client.services().get(name=f'projects/{self.project_id}/services/{service_name}')
            response = request.execute()
            is_enabled = response.get('state') == 'ENABLED'
            logger.info(f"Service '{service_name}' enabled: {is_enabled}")
            return is_enabled
        except HttpError as e:
            if e.resp.status == 404:
                logger.info(f"Service '{service_name}' not found.")
                return False
            elif e.resp.status == 403:
                logger.error(f"Permission denied while checking service '{service_name}': {e}")
                return False
            else:
                logger.error(f"Error checking status of service '{service_name}': {e}")
                raise

    def enable_service(self, service_name: str):
        """
        Enables a specific GCP service for the project.
        """
        try:
            service_full_name = f"projects/{self.project_id}/services/{service_name}"
            request = self.service_client.services().enable(name=service_full_name)
            operation = request.execute()
            logger.info(f"Enabling service '{service_name}'...")
            self._wait_for_operation(operation)
            logger.info(f"Service '{service_name}' enabled successfully.")
        except HttpError as e:
            if e.resp.status == 403:
                logger.error(f"Permission denied while enabling service '{service_name}': {e}")
            else:
                logger.error(f"Failed to enable service '{service_name}': {e}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error while enabling service '{service_name}': {e}")
            raise

    def _wait_for_operation(self, operation):
        """
        Waits for a long-running operation to complete.
        """
        operation_name = operation.get('name')
        if not operation_name:
            logger.error("No operation name found in the response.")
            return

        while not operation.get('done', False):
            logger.debug(f"Waiting for operation '{operation_name}' to complete...")
            time.sleep(5)  # Increased interval to 5 seconds for better performance
            try:
                operation = self.service_client.operations().get(name=operation_name).execute()
            except HttpError as e:
                logger.error(f"Error while waiting for operation '{operation_name}': {e}")
                raise
            except Exception as e:
                logger.exception(f"Unexpected error while waiting for operation '{operation_name}': {e}")
                raise

    def enable_required_services(self):
        required_services = [
            'compute.googleapis.com',
            'cloudfunctions.googleapis.com',
            'run.googleapis.com',
            'monitoring.googleapis.com',
            'logging.googleapis.com',
            'baremetalsolution.googleapis.com',
            'anthos.googleapis.com',
            'storage.googleapis.com',
            'bigquery.googleapis.com',
            'dataflow.googleapis.com',
            'aiplatform.googleapis.com',
            'pubsub.googleapis.com'
        ]
        self.ensure_services_enabled(required_services)