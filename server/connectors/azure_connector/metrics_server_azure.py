import logging, time, requests
from kubernetes import client
import yaml

def ensure_kubernetes_api(subscription_id, access_token):
    """
    Check if Kubernetes API is enabled for the subscription.
    Uses the provided access_token instead of DefaultAzureCredential.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.ContainerService?api-version=2024-11-01"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        providers = response.json().get("value", [])
        for provider in providers:
            if provider.get("registrationState") != "Registered":
                logging.info("Kubernetes API not registered. Registering now...")
                register_kubernetes_api(subscription_id, access_token)
                time.sleep(5)  # Allow some time for registration
    else:
        logging.error(f"Failed to check Kubernetes API: {response.text}")

def register_kubernetes_api(subscription_id, access_token):
    """Register Kubernetes API for the subscription with retry logic."""
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.ContainerService/register?api-version=2021-05-01"
    for attempt in range(3):
        response = requests.post(url, headers=headers)
        if response.status_code == 200:
            logging.info("Successfully registered Kubernetes API.")
            return
        else:
            logging.error(f"Attempt {attempt+1} to register Kubernetes API failed: {response.text}")
            time.sleep(10)
    logging.error("Failed to register Kubernetes API after 3 attempts.")

def is_metrics_server_installed(api_client):
    """Check if the Metrics Server is installed in the AKS cluster."""
    try:
        apps_v1 = client.AppsV1Api(api_client)
        deployments = apps_v1.list_namespaced_deployment(namespace="kube-system")
        for deployment in deployments.items:
            if "metrics-server-azure" in deployment.metadata.name:
                logging.info("Metrics Server is already installed.")
                return True
        logging.info("Metrics Server not found.")
        return False
    except Exception as e:
        logging.error(f"Error checking for Metrics Server: {e}")
        return False

def deploy_metrics_server(api_client):
    """Deploy the Metrics Server using the provided Kubernetes API client."""
    try:
        yaml_url = "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
        response = requests.get(yaml_url)
        response.raise_for_status()
        resources = list(yaml.safe_load_all(response.text))

        apps_v1 = client.AppsV1Api(api_client)
        core_v1 = client.CoreV1Api(api_client)
        rbac_v1 = client.RbacAuthorizationV1Api(api_client)

        for resource in resources:
            kind = resource.get("kind", "").lower()
            metadata = resource.get("metadata", {})
            name = metadata.get("name", "unknown-resource")
            try:
                if kind == "deployment":
                    logging.info(f"Creating deployment: {name}")
                    apps_v1.create_namespaced_deployment(namespace="kube-system", body=resource)
                elif kind == "service":
                    logging.info(f"Creating service: {name}")
                    core_v1.create_namespaced_service(namespace="kube-system", body=resource)
                elif kind == "serviceaccount":
                    logging.info(f"Creating service account: {name}")
                    core_v1.create_namespaced_service_account(namespace="kube-system", body=resource)
                elif kind == "role":
                    logging.info(f"Creating role: {name}")
                    rbac_v1.create_namespaced_role(namespace="kube-system", body=resource)
                elif kind == "rolebinding":
                    logging.info(f"Creating role binding: {name}")
                    rbac_v1.create_namespaced_role_binding(namespace="kube-system", body=resource)
            except Exception as ex:
                # If the error indicates the resource already exists, log and continue.
                if "already exists" in str(ex).lower():
                    logging.info(f"{kind.capitalize()} '{name}' already exists, skipping.")
                else:
                    # Re-raise if it is a different error
                    raise

        logging.info("Metrics Server deployment completed successfully.")
    except Exception as e:
        logging.error(f"Failed to deploy Metrics Server: {e}")

