"""
Resource Mapper - Maps cloud provider resource types to graph node types.
Used by all Phase 1 discovery providers to normalize resource types.
"""

# =========================================================================
# GCP Resource Type Mappings
# =========================================================================

GCP_RESOURCE_MAP = {
    "compute.googleapis.com/Instance": ("vm", "gce"),
    "compute.googleapis.com/InstanceGroup": ("vm", "instance_group"),
    "container.googleapis.com/Cluster": ("kubernetes_cluster", "gke"),
    "sqladmin.googleapis.com/Instance": ("database", "cloud_sql"),
    "spanner.googleapis.com/Instance": ("database", "spanner"),
    "alloydb.googleapis.com/Cluster": ("database", "alloydb"),
    "firestore.googleapis.com/Database": ("database", "firestore"),
    "bigtableadmin.googleapis.com/Instance": ("database", "bigtable"),
    "bigquery.googleapis.com/Dataset": ("database", "bigquery"),
    "redis.googleapis.com/Instance": ("cache", "memorystore_redis"),
    "memcache.googleapis.com/Instance": ("cache", "memorystore_memcached"),
    "compute.googleapis.com/ForwardingRule": ("load_balancer", "gcp_lb"),
    "compute.googleapis.com/BackendService": ("load_balancer", "gcp_backend"),
    "compute.googleapis.com/UrlMap": ("load_balancer", "gcp_url_map"),
    "cloudfunctions.googleapis.com/Function": ("serverless_function", "cloud_function"),
    "run.googleapis.com/Service": ("serverless_function", "cloud_run"),
    "appengine.googleapis.com/Service": ("serverless_function", "app_engine"),
    "pubsub.googleapis.com/Topic": ("message_queue", "pubsub_topic"),
    "pubsub.googleapis.com/Subscription": ("message_queue", "pubsub_subscription"),
    "cloudtasks.googleapis.com/Queue": ("message_queue", "cloud_tasks"),
    "storage.googleapis.com/Bucket": ("storage_bucket", "gcs"),
    "dns.googleapis.com/ManagedZone": ("dns_zone", "cloud_dns"),
    "compute.googleapis.com/Network": ("vpc", "gcp_vpc"),
    "compute.googleapis.com/Subnetwork": ("subnet", "gcp_subnet"),
    "compute.googleapis.com/Firewall": ("firewall", "gcp_firewall"),
    "secretmanager.googleapis.com/Secret": ("secret_store", "gcp_secret_manager"),
    "file.googleapis.com/Instance": ("filesystem", "filestore"),
    "dataflow.googleapis.com/Job": ("data_pipeline", "dataflow"),
    "dataproc.googleapis.com/Cluster": ("data_pipeline", "dataproc"),
    "composer.googleapis.com/Environment": ("data_pipeline", "composer"),
}

# =========================================================================
# AWS Resource Type Mappings
# =========================================================================

AWS_RESOURCE_MAP = {
    "AWS::EC2::Instance": ("vm", "ec2"),
    "AWS::AutoScaling::AutoScalingGroup": ("vm", "asg"),
    "AWS::ECS::Cluster": ("vm", "ecs_cluster"),
    "AWS::ECS::Service": ("serverless_function", "ecs_service"),
    "AWS::EKS::Cluster": ("kubernetes_cluster", "eks"),
    "AWS::RDS::DBInstance": ("database", "rds"),
    "AWS::RDS::DBCluster": ("database", "aurora_rds"),
    "AWS::DynamoDB::Table": ("database", "dynamodb"),
    "AWS::DocDB::DBCluster": ("database", "documentdb"),
    "AWS::Neptune::DBCluster": ("database", "neptune"),
    "AWS::Redshift::Cluster": ("database", "redshift"),
    "AWS::Timestream::Database": ("database", "timestream"),
    "AWS::Cassandra::Keyspace": ("database", "keyspaces"),
    "AWS::ElastiCache::CacheCluster": ("cache", "elasticache"),
    "AWS::ElastiCache::ReplicationGroup": ("cache", "elasticache_repl"),
    "AWS::MemoryDB::Cluster": ("cache", "memorydb"),
    "AWS::ElasticLoadBalancingV2::LoadBalancer": ("load_balancer", "alb_nlb"),
    "AWS::ElasticLoadBalancing::LoadBalancer": ("load_balancer", "clb"),
    "AWS::Lambda::Function": ("serverless_function", "lambda"),
    "AWS::ApiGateway::RestApi": ("api_gateway", "api_gateway_rest"),
    "AWS::ApiGatewayV2::Api": ("api_gateway", "api_gateway_http"),
    "AWS::StepFunctions::StateMachine": ("serverless_function", "step_function"),
    "AWS::SQS::Queue": ("message_queue", "sqs"),
    "AWS::SNS::Topic": ("message_queue", "sns"),
    "AWS::Kinesis::Stream": ("message_queue", "kinesis"),
    "AWS::Events::Rule": ("message_queue", "eventbridge"),
    "AWS::MSK::Cluster": ("message_queue", "kafka_msk"),
    "AWS::AmazonMQ::Broker": ("message_queue", "amazon_mq"),
    "AWS::S3::Bucket": ("storage_bucket", "s3"),
    "AWS::EFS::FileSystem": ("filesystem", "efs"),
    "AWS::OpenSearchService::Domain": ("search_engine", "opensearch"),
    "AWS::Route53::HostedZone": ("dns_zone", "route53"),
    "AWS::CloudFront::Distribution": ("cdn", "cloudfront"),
    "AWS::EC2::VPC": ("vpc", "aws_vpc"),
    "AWS::EC2::Subnet": ("subnet", "aws_subnet"),
    "AWS::EC2::SecurityGroup": ("firewall", "security_group"),
    "AWS::SecretsManager::Secret": ("secret_store", "secrets_manager"),
    "AWS::SSM::Parameter": ("secret_store", "parameter_store"),
    "AWS::EMR::Cluster": ("data_pipeline", "emr"),
    "AWS::Glue::Job": ("data_pipeline", "glue"),
    "AWS::ServiceDiscovery::Namespace": ("service_discovery", "cloudmap"),
}

# =========================================================================
# Azure Resource Type Mappings
# =========================================================================

AZURE_RESOURCE_MAP = {
    "microsoft.compute/virtualmachines": ("vm", "azure_vm"),
    "microsoft.compute/virtualmachinescalesets": ("vm", "vmss"),
    "microsoft.containerinstance/containergroups": ("vm", "aci"),
    "microsoft.containerservice/managedclusters": ("kubernetes_cluster", "aks"),
    "microsoft.sql/servers/databases": ("database", "azure_sql"),
    "microsoft.dbforpostgresql/flexibleservers": ("database", "azure_postgresql"),
    "microsoft.dbformysql/flexibleservers": ("database", "azure_mysql"),
    "microsoft.documentdb/databaseaccounts": ("database", "cosmos_db"),
    "microsoft.cache/redis": ("cache", "azure_redis"),
    "microsoft.network/loadbalancers": ("load_balancer", "azure_lb"),
    "microsoft.network/applicationgateways": ("load_balancer", "app_gateway"),
    "microsoft.network/frontdoors": ("cdn", "front_door"),
    "microsoft.web/sites": ("serverless_function", "app_service"),
    "microsoft.app/containerapps": ("serverless_function", "container_app"),
    "microsoft.logic/workflows": ("serverless_function", "logic_app"),
    "microsoft.apimanagement/service": ("api_gateway", "azure_apim"),
    "microsoft.servicebus/namespaces": ("message_queue", "service_bus"),
    "microsoft.eventhub/namespaces": ("message_queue", "event_hub"),
    "microsoft.eventgrid/topics": ("message_queue", "event_grid"),
    "microsoft.storage/storageaccounts": ("storage_bucket", "azure_blob"),
    "microsoft.search/searchservices": ("search_engine", "azure_search"),
    "microsoft.network/dnszones": ("dns_zone", "azure_dns"),
    "microsoft.network/virtualnetworks": ("vpc", "azure_vnet"),
    "microsoft.network/networksecuritygroups": ("firewall", "nsg"),
    "microsoft.keyvault/vaults": ("secret_store", "key_vault"),
    "microsoft.datafactory/factories": ("data_pipeline", "data_factory"),
    "microsoft.databricks/workspaces": ("data_pipeline", "databricks"),
    "microsoft.hdinsight/clusters": ("data_pipeline", "hdinsight"),
}


def map_gcp_resource(asset_type):
    """Map a GCP asset type to (resource_type, sub_type) tuple."""
    return GCP_RESOURCE_MAP.get(asset_type, (None, None))


def map_aws_resource(resource_type_str):
    """Map an AWS resource type to (resource_type, sub_type) tuple."""
    return AWS_RESOURCE_MAP.get(resource_type_str, (None, None))


def map_azure_resource(azure_type, kind=None):
    """Map an Azure resource type to (resource_type, sub_type) tuple.
    Handles the special case of microsoft.web/sites having different kinds.
    """
    normalized = azure_type.lower()
    if normalized == "microsoft.web/sites" and kind:
        kind_lower = kind.lower()
        if "functionapp" in kind_lower:
            return ("serverless_function", "azure_function")
        return ("serverless_function", "app_service")
    return AZURE_RESOURCE_MAP.get(normalized, (None, None))


# =========================================================================
# K8s Image-to-Type Heuristics
# =========================================================================

IMAGE_TYPE_HEURISTICS = {
    "postgres": ("database", "postgresql"),
    "mysql": ("database", "mysql"),
    "mariadb": ("database", "mariadb"),
    "mongo": ("database", "mongodb"),
    "cockroach": ("database", "cockroachdb"),
    "redis": ("cache", "redis"),
    "memcache": ("cache", "memcached"),
    "valkey": ("cache", "valkey"),
    "rabbitmq": ("message_queue", "rabbitmq"),
    "kafka": ("message_queue", "kafka"),
    "nats": ("message_queue", "nats"),
    "pulsar": ("message_queue", "pulsar"),
    "nginx": ("load_balancer", "nginx"),
    "haproxy": ("load_balancer", "haproxy"),
    "traefik": ("load_balancer", "traefik"),
    "envoy": ("load_balancer", "envoy"),
    "caddy": ("load_balancer", "caddy"),
    "minio": ("storage_bucket", "minio"),
    "seaweed": ("storage_bucket", "seaweedfs"),
}


def infer_type_from_image(image_name):
    """Infer resource_type and sub_type from a container image name."""
    if not image_name:
        return None, None
    image_lower = image_name.lower()
    for prefix, (rtype, stype) in IMAGE_TYPE_HEURISTICS.items():
        if prefix in image_lower:
            return rtype, stype
    return None, None


# =========================================================================
# Port-to-Dependency Type Mapping
# =========================================================================

PORT_DEPENDENCY_MAP = {
    5432: ("database", "postgresql"),
    3306: ("database", "mysql"),
    27017: ("database", "mongodb"),
    6379: ("cache", "redis"),
    11211: ("cache", "memcached"),
    5672: ("queue", "rabbitmq"),
    15672: ("queue", "rabbitmq"),
    9092: ("queue", "kafka"),
    80: ("http", None),
    443: ("http", None),
    8080: ("http", None),
    8443: ("http", None),
    50051: ("grpc", None),
}


def infer_dependency_type_from_port(port):
    """Infer dependency_type from a port number. Returns (dependency_type, sub_type)."""
    return PORT_DEPENDENCY_MAP.get(port, ("network", None))


# =========================================================================
# GCP Relationship Type -> Dependency Type Mapping
# =========================================================================

GCP_RELATIONSHIP_TYPE_MAP = {
    "COMPUTE_INSTANCE_TO_INSTANCEGROUP": "network",
    "GKE_CLUSTER_TO_NODEPOOL": "network",
    "CLOUD_SQL_INSTANCE_TO_VPC_NETWORK": "network",
    "CLOUD_FUNCTION_TO_PUBSUB_TOPIC": "event_trigger",
    "COMPUTE_INSTANCE_TO_VPC_NETWORK": "network",
    "COMPUTE_INSTANCE_TO_SUBNETWORK": "network",
    "COMPUTE_INSTANCE_TO_DISK": "storage",
    "CLOUD_RUN_SERVICE_TO_VPC_CONNECTOR": "network",
    "GKE_CLUSTER_TO_VPC_NETWORK": "network",
    "CLOUD_SQL_INSTANCE_TO_BACKUP": "storage",
    "PUBSUB_SUBSCRIPTION_TO_TOPIC": "event_trigger",
    "COMPUTE_FORWARDING_RULE_TO_BACKEND_SERVICE": "network",
    "COMPUTE_BACKEND_SERVICE_TO_INSTANCE_GROUP": "network",
    "COMPUTE_URL_MAP_TO_BACKEND_SERVICE": "network",
}
