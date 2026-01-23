from flask import Flask, redirect, request, session, jsonify
import requests
from google.cloud import billing_v1
from google.cloud import bigquery
from connectors.gcp_connector.auth.oauth import get_credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from utils.db.db_utils import connect_to_db_as_admin
import psycopg2
from psycopg2 import DatabaseError
import logging
from psycopg2.extras import execute_batch
import json

def categorize_bigquery_data(service, sku):
    """
    Categorizes BigQuery billing data based on predefined service categories.

    Parameters:
        Service and SKU descriptions of BigQuery entry

    Returns:
        Category of input data
    """
    data_category = "Uncategorized"
    categories = {
        "Compute": [
            "Compute Engine", "NVIDIA GPUs", "Cloud TPU", "Sole-Tenant Nodes",
            "Bare Metal Solutions", "Cloud Functions", "Cloud Run",
            "Committed Use Discounts for Compute Engine", "E2 Instances",
            "Edge TPU Devices", "Anthos", "Custom VM Types",
            "Preemptible VMs", "Local SSDs", "Spot VMs", "Google Kubernetes Engine (GKE)", "Kubernetes Engine"
        ],
        "Storage": [
            "Persistent Disk (PD-SSD)", "Cloud Storage", "Cloud Storage Archive",
            "Persistent Disk (Extreme PD)", "Cloud SQL", "Cloud Spanner",
            "Transfer Appliance", "Storage Transfer Service", "Filestore",
            "Persistent Disk Snapshots", "Cloud Storage Lifecycle Management",
            "Cloud Storage Standard Tier", "Transfer Appliance", "Artifact Registry"
        ], #If this list is updated, also update the list in the query / Might want to use a variable later 
        "Networking": [
            "Network Egress", "Inter-Region Network Egress", "Cloud CDN",
            "Cloud VPN", "Private Google Access", "Private Service Connect",
            "Cloud Load Balancing", "Cloud Interconnect", "VPC Network Peering",
            "Cloud Firewall Rules", "Anthos Service Mesh", "Traffic Director", "Networking"
        ],
        "Specialized Services": [
            "BigQuery", "Dataflow",
            "AI Platform Training", "Vertex AI", "Pub/Sub",
            "Cloud IoT Core", "API Gateway", "Google Quantum AI",
            "Cloud Security Command Center", "Policy Analyzer", "Cloud Logging", "Cloud Monitoring"
        ]
    }

    categorized = False
    for category, keywords in categories.items():
        for keyword in keywords:
            if keyword.lower() in service or keyword.lower() in sku:
                data_category = category
                categorized = True
                break
        if categorized:
            break

    return data_category

def list_tables_in_dataset(credentials, project_id, dataset_id):
    client = bigquery.Client(project=project_id, credentials=credentials)
    dataset_ref = client.dataset(dataset_id)
    tables = client.list_tables(dataset_ref)
    table_names = [table.table_id for table in tables]
    return table_names

def is_bigquery_enabled(project_id, credentials):
    """
    Check if BigQuery API is enabled for the given project.
    """
    try:
        service = build('serviceusage', 'v1', credentials=credentials)
        
        # Check if BigQuery API is enabled
        response = service.services().get(
            name=f'projects/{project_id}/services/bigquery.googleapis.com'
        ).execute()
        
        if response.get('state') == 'ENABLED':
            return True
        return False
    
    except HttpError as e:
        if e.resp.status == 403:
            raise ValueError("Permission Denied: Ensure the credentials have the proper permissions.")
        if e.resp.status == 404:
            return False
        raise
    except Exception as e:
        raise ValueError(f"Failed to check BigQuery activation: {str(e)}")

def store_bigquery_data(credentials, project_id, user_id):
    """
    Fetches and stores BigQuery billing and usage data into PostgreSQL 'cloud_billing_usage' table.
    """

    logging.info(f"Fetching BigQuery data for project '{project_id}'...")
    client = bigquery.Client(project=project_id, location="US", credentials=credentials)
    datasets = client.list_datasets()
    dataset_ids = [dataset.dataset_id for dataset in datasets]

    cloud_billing_data = []

    for dataset_id in dataset_ids:
        table_names = list_tables_in_dataset(credentials, project_id, dataset_id)
        for table in table_names:
            if "gcp_billing_export" in table:
                cloud_billing_query = f"""
                SELECT
                    service.description AS service,
                    sku.description AS sku,
                    SUM(cost) AS total_cost,
                    usage.unit AS unit,
                    SUM(usage.amount) AS amount_used,
                    CAST(usage_start_time AS DATE) AS usage_date,
                    currency
                FROM `{project_id}.{dataset_id}.{table}`
                WHERE cost >= 0.005
                GROUP BY 1, 2, 4, 6, 7
                ORDER BY total_cost DESC
                """
                cloud_billing_job = client.query(cloud_billing_query)
                for row in cloud_billing_job.result():
                    category = categorize_bigquery_data(row['service'].lower(), row['sku'].lower())
                    record = {
                        'service': row['service'],
                        'sku': row['sku'],
                        'category': category,
                        'cost': float(row['total_cost']),
                        'usage': float(row['amount_used']),
                        'unit': row['unit'],
                        'usage_date': row['usage_date'],
                        'region': 'us-central1',
                        'project_id': project_id,
                        'currency': row['currency'],
                        'dataset_id': dataset_id,
                        'table_name': table,
                        'user_id': user_id,
                        'provider': 'gcp'
                    }
                    cloud_billing_data.append(record)

    if cloud_billing_data:
        conn = None
        cursor = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            insert_query = """
                INSERT INTO cloud_billing_usage (
                    service, sku, category, cost, usage, unit,
                    usage_date, region, project_id, currency, dataset_id, table_name, user_id, provider, timestamp
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, DEFAULT
                )
                ON CONFLICT (service, sku, category, usage_date, region, project_id, dataset_id, user_id)
                DO UPDATE SET 
                    cost = EXCLUDED.cost,
                    usage = EXCLUDED.usage,
                    unit = EXCLUDED.unit,
                    currency = EXCLUDED.currency,
                    provider = EXCLUDED.provider,
                    timestamp = CURRENT_TIMESTAMP;
            """

            for idx, row in enumerate(cloud_billing_data):
                try:
                    data_tuple = (
                        row.get('service', 'unknown'),
                        row.get('sku', 'unknown'),
                        row.get('category', 'Uncategorized'),
                        row.get('cost', 0.0),
                        row.get('usage', 0.0),
                        row.get('unit', 'unknown'),
                        row.get('usage_date'),
                        row.get('region', 'unknown'),
                        row.get('project_id', 'unknown'),
                        row.get('currency', 'USD'),
                        row.get('dataset_id', 'unknown'),
                        row.get('table_name', 'unknown'),
                        row.get('user_id', 'unknown'),
                        row.get('provider', 'gcp')
                    )
                    cursor.execute(insert_query, data_tuple)
                except Exception as ex:
                    logging.error("Error inserting row %d: %s", idx, row)
                    logging.exception(ex)
                    raise
            conn.commit()
            logging.info(f"Successfully stored {len(cloud_billing_data)} rows in 'cloud_billing_usage' table.")
        except Exception as e:
            logging.error(f"Error storing BigQuery data in PostgreSQL: {e}")
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
                logging.debug(" Database connection closed.")

def has_active_billing(project_id, credentials):
    """
    Check if a GCP project has active billing enabled.
    
    Args:
        project_id: The GCP project ID
        credentials: GCP credentials
        
    Returns:
        bool: True if billing is active, False otherwise
    """
    try:
        logging.info(f" Checking billing status for project: {project_id}")
        
        # Build the Cloud Billing API client
        service = build('cloudbilling', 'v1', credentials=credentials)
        logging.info(" Successfully built Cloud Billing API client")
        
        # Get the project's billing info
        project_name = f'projects/{project_id}'
        logging.info(f" Fetching billing info for project: {project_name}")
        response = service.projects().getBillingInfo(name=project_name).execute()
        
        # Check if billing is enabled
        billing_enabled = response.get('billingEnabled', False)
        billing_account = response.get('billingAccountName')
        
        logging.info(f" Billing status for {project_id}:")
        logging.info(f"  - Billing enabled: {billing_enabled}")
        logging.info(f"  - Billing account: {billing_account}")
        
        if billing_enabled and billing_account:
            logging.info(f" Project {project_id} has active billing account: {billing_account}")
            return True
            
        logging.warning(f" Project {project_id} does not have active billing")
        return False
        
    except HttpError as e:
        if e.resp.status == 403:
            logging.error(f" Permission denied while checking billing for project {project_id}")
            return False
        elif e.resp.status == 404:
            logging.warning(f" Billing info not found for project {project_id}")
            return False
        else:
            logging.error(f" Error checking billing status for project {project_id}: {e}")
            return False
    except Exception as e:
        logging.error(f" Unexpected error checking billing status for project {project_id}: {e}")
        return False

def find_project_with_billing(credentials):
    """
    Find a GCP project with active billing.
    
    Args:
        credentials: GCP credentials
            
    Returns:
        str: Project ID with active billing
            
    Raises:
        ValueError: If no project with active billing is found
    """
    try:
        from connectors.gcp_connector.gcp.projects import get_project_list
        
        # Get list of projects accessible with these credentials
        projects = get_project_list(credentials)
        
        if not projects:
            raise ValueError("No GCP projects found for this account. Please see https://cloud.google.com/docs and set up a project.")
            
        logging.info(f" Found {len(projects)} GCP projects. Checking for active billing...")
        
        # Look for projects with active billing
        for project in projects:
            project_id = project.get('projectId')
            if not project_id:
                continue
                
            if has_active_billing(project_id, credentials):
                logging.info(f" Found project with active billing: {project_id}")
                return project_id
                
        # No project with active billing found
        raise ValueError("No GCP project with active billing found. Please enable billing on a project. Please see https://cloud.google.com/docs and attach a billing account.")
        
    except Exception as e:
        error_msg = f"Failed to find project with active billing: {str(e)}"
        logging.error(f" {error_msg}")
        raise ValueError(error_msg)



