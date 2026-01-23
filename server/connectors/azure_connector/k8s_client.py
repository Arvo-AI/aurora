import logging
import base64
import re
import requests
from kubernetes import client
from kubernetes import config as k8s_config
from azure.identity import ClientSecretCredential

def extract_resource_group(resource_id):
    match = re.search(r"/resourceGroups/([^/]+)", resource_id, re.IGNORECASE)
    return match.group(1) if match else None

def get_aks_clusters(management_token, subscription_id, service_principal_id, tenant_id, client_id, client_secret):
    """
    Fetch all AKS clusters and create a Kubernetes client for each.
    If static credentials are disabled, fall back to acquiring an AAD token for AKS.
    """
    from connectors.azure_connector.metrics_server_azure import ensure_kubernetes_api, is_metrics_server_installed, deploy_metrics_server

    try:
        if not management_token or not subscription_id:
            logging.error("Missing management token or subscription ID!")
            return []

        # Ensure Kubernetes API is registered (optional)
        ensure_kubernetes_api(subscription_id, management_token)

        aks_url = f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.ContainerService/managedClusters?api-version=2023-06-01"
        headers = {"Authorization": f"Bearer {management_token}"}
        response = requests.get(aks_url, headers=headers)
        if response.status_code != 200:
            logging.error(f"Failed to fetch AKS clusters: {response.text}")
            return []

        clusters = response.json().get("value", [])
        for cluster in clusters:
            cluster_name = cluster["name"]
            resource_id = cluster["id"]
            resource_group = extract_resource_group(resource_id)
            if not resource_group:
                logging.warning(f"Could not extract resource group for cluster {cluster_name}")
                continue

            logging.info(f"Found cluster: {cluster_name} in resource group: {resource_group}")

            # Try to get static admin credentials first (works best for non-Azure AD clusters)
            admin_creds_url = f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.ContainerService/managedClusters/{cluster_name}/listClusterAdminCredential?api-version=2023-06-01"
            logging.info(f"Requesting admin credentials from: {admin_creds_url}")
            admin_resp = requests.post(admin_creds_url, headers=headers)
            logging.info(f"Admin credentials response status: {admin_resp.status_code}")
            if admin_resp.status_code != 200:
                logging.error(f"Admin credentials failed: {admin_resp.text}")
            if admin_resp.status_code == 200:
                kubeconfig_b64 = admin_resp.json()["kubeconfigs"][0]["value"]
                kubeconfig = base64.b64decode(kubeconfig_b64).decode("utf-8")
                # Create Kubernetes client using the kubeconfig file
                api_client = get_k8s_client_from_kubeconfig(kubeconfig)
                logging.info(f"Successfully created Kubernetes client using admin credentials for {cluster_name}")
            else:
                # Static credentials not allowed: fall back to AAD token
                logging.warning(f"Static credentials disabled for {cluster_name}, falling back to AAD token.")
                # Acquire token for AKS using the well-known resource ID for AKS:
                # 6dae42f8-4368-4678-94ff-3960e28e3630 is the resource ID for AKS.
                credential = ClientSecretCredential(tenant_id, client_id, client_secret)
                aks_token = credential.get_token("6dae42f8-4368-4678-94ff-3960e28e3630/.default").token
                # Create Kubernetes client using this token and the cluster's fqdn
                fqdn = cluster["properties"]["fqdn"]
                api_client = get_k8s_client(fqdn, aks_token)
                logging.info(f"Created Kubernetes client using AAD token for {cluster_name}")

            cluster["api_client"] = api_client


            # Optionally, deploy metrics server if not installed
            if api_client and not is_metrics_server_installed(api_client):
                logging.info(f"Metrics Server not found for {cluster_name}. Deploying...")
                deploy_metrics_server(api_client)

        logging.info(f"Successfully processed {len(clusters)} AKS clusters.")
        return clusters
    except Exception as e:
        logging.error(f" Error fetching AKS clusters: {e}")
        return []

def get_k8s_client(endpoint, access_token):
    try:
        logging.info("Creating Kubernetes client using AAD token...")
        configuration = client.Configuration()
        configuration.host = f"https://{endpoint}"
        # Disable SSL verification for AKS clusters to avoid certificate issues in containerized environments  
        configuration.verify_ssl = False
        configuration.api_key = {"authorization": access_token}
        configuration.api_key_prefix = {"authorization": "Bearer"}
        return client.ApiClient(configuration)
    except Exception as e:
        logging.error(f"Error configuring Kubernetes client: {e}")
        return None


def get_k8s_client_from_kubeconfig(kubeconfig_str):
    """Create a Kubernetes API client using a kubeconfig string."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmp:
        tmp.write(kubeconfig_str)
        tmp.flush()
        k8s_config.load_kube_config(config_file=tmp.name)
        return client.ApiClient()


def get_sp_object_id(tenant_id, client_id, client_secret):
    # Step 1: Get Graph API token
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    token_data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default"
    }

    token_response = requests.post(token_url, data=token_data)
    token_response.raise_for_status()
    access_token = token_response.json()["access_token"]

    # Step 2: Query Graph API for service principal objectId
    headers = {"Authorization": f"Bearer {access_token}"}
    graph_url = f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{client_id}'"
    
    response = requests.get(graph_url, headers=headers)
    response.raise_for_status()
    results = response.json().get("value", [])
    
    if not results:
        raise Exception("Service principal not found.")

    object_id = results[0]["id"]
    return object_id
