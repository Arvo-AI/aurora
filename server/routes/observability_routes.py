"""
Observability API Routes - /api/observability/* endpoints for infrastructure overview.
"""

import json
import logging
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from services.graph.memgraph_client import get_memgraph_client
from services.discovery.resource_mapper import (
    RESOURCE_TYPE_TO_CATEGORY,
    get_category,
    normalize_status,
)
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

observability_bp = Blueprint("observability", __name__, url_prefix="/api/observability")


@observability_bp.route("/summary", methods=["GET"])
@require_permission("graph", "read")
def get_summary(user_id):
    """GET /api/observability/summary - Aggregated counts for dashboard cards."""
    client = get_memgraph_client()
    stats = client.get_graph_stats(user_id)

    status_counts_raw = client.get_services_status_counts(user_id)

    by_status = {}
    for raw_status, count in status_counts_raw.items():
        display = normalize_status(raw_status)
        by_status[display] = by_status.get(display, 0) + count

    by_category = {}
    for rtype, count in stats.get("services_by_type", {}).items():
        cat = get_category(rtype)
        by_category[cat] = by_category.get(cat, 0) + count

    onprem_count = 0
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT (SELECT count(*) FROM user_manual_vms WHERE user_id = %s) + (SELECT count(*) FROM user_onprem_resources WHERE user_id = %s)",
                    (user_id, user_id),
                )
                row = cur.fetchone()
                onprem_count = row[0] if row else 0
    except Exception as e:
        logger.warning("Failed to count on-prem resources: %s", e)

    by_provider = stats.get("services_by_provider", {})
    if onprem_count > 0:
        by_provider["onprem"] = by_provider.get("onprem", 0) + onprem_count
        onprem_category = get_category("vm")
        by_category[onprem_category] = by_category.get(onprem_category, 0) + onprem_count

    total = stats.get("total_services", 0) + onprem_count

    return jsonify({
        "total_resources": total,
        "by_provider": by_provider,
        "by_category": by_category,
        "by_status": by_status,
    }), 200


@observability_bp.route("/resources", methods=["GET"])
@require_permission("graph", "read")
def list_resources(user_id):
    """GET /api/observability/resources - Paginated, filterable resource list."""
    provider = request.args.get("provider")
    category = request.args.get("category")
    resource_type = request.args.get("resource_type")
    status_filter = request.args.get("status")
    search = request.args.get("search")
    page = max(1, int(request.args.get("page", 1)))
    limit = min(100, max(1, int(request.args.get("limit", 50))))
    skip = (page - 1) * limit

    client = get_memgraph_client()

    resource_types_for_category = None
    if category and not resource_type:
        resource_types_for_category = [
            rt for rt, cat in RESOURCE_TYPE_TO_CATEGORY.items() if cat == category
        ]

    if resource_types_for_category:
        all_filtered = []
        for rt in resource_types_for_category:
            svcs = client.list_services_paginated(
                user_id, resource_type=rt, provider=provider,
                search=search, skip=0, limit=10000,
            )
            all_filtered.extend(svcs)
        if status_filter:
            all_filtered = [
                s for s in all_filtered
                if normalize_status(s.get("status", "")) == status_filter
            ]
        total = len(all_filtered)
        all_filtered.sort(key=lambda s: s.get("name", ""))
        resources = all_filtered[skip:skip + limit]
    else:
        resources = client.list_services_paginated(
            user_id,
            resource_type=resource_type,
            provider=provider if provider != "onprem" else None,
            status=None,
            search=search,
            skip=skip,
            limit=limit,
        )
        total = client.count_services(
            user_id,
            resource_type=resource_type,
            provider=provider if provider != "onprem" else None,
            search=search,
        )

    onprem_resources = []
    if not provider or provider == "onprem":
        try:
            onprem_resources = _get_onprem_resources(user_id, search)
        except Exception as e:
            logger.warning("Failed to fetch on-prem resources: %s", e)

    all_resources = []
    for svc in resources:
        all_resources.append(_normalize_resource(svc))

    for vm in onprem_resources:
        all_resources.append({
            "id": f"onprem:{vm['name']}",
            "name": vm["name"],
            "display_name": vm["name"],
            "resource_type": "vm",
            "sub_type": "manual",
            "category": "Compute",
            "provider": "onprem",
            "region": "",
            "status": "Unknown",
            "cloud_resource_id": "",
            "endpoint": f"{vm.get('ip_address', '')}:{vm.get('port', 22)}",
            "updated_at": str(vm.get("updated_at", "")),
        })

    if provider == "onprem":
        total = len(onprem_resources)
        all_resources = all_resources[skip:skip + limit]
    elif onprem_resources:
        total = total + len(onprem_resources)

    total_pages = max(1, (total + limit - 1) // limit)

    return jsonify({
        "resources": all_resources,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }), 200


@observability_bp.route("/resources/<path:resource_id>", methods=["GET"])
@require_permission("graph", "read")
def get_resource_detail(user_id, resource_id):
    """GET /api/observability/resources/<resource_id> - Single resource detail."""
    client = get_memgraph_client()

    # Try to find by name (resource_id is the service name)
    service = client.get_service(user_id, resource_id)
    if not service:
        return jsonify({"error": "Resource not found"}), 404

    resource = _normalize_resource(service)
    resource["upstream"] = service.get("upstream", [])
    resource["downstream"] = service.get("downstream", [])

    # Get impact radius
    try:
        impact = client.get_impact_radius(user_id, resource_id)
        resource["impact"] = impact.get("impact", {})
        resource["total_affected"] = impact.get("total_affected", 0)
    except Exception as e:
        logger.warning("Failed to get impact radius: %s", e)
        resource["impact"] = {}
        resource["total_affected"] = 0

    # Fetch related incidents from PostgreSQL
    resource["incidents"] = _get_related_incidents(user_id, resource_id)

    # Fetch active alerts from monitoring tables
    resource["alerts"] = _get_related_alerts(user_id, resource_id)

    # Fetch K8s workloads if this is a cluster
    if service.get("resource_type") == "kubernetes_cluster":
        resource["k8s_workloads"] = _get_k8s_workloads(
            user_id, service.get("name", "")
        )

    return jsonify(resource), 200


@observability_bp.route("/onprem", methods=["POST"])
@require_permission("graph", "write")
def register_onprem_resource(user_id):
    """POST /api/observability/onprem - Register an on-prem resource."""
    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"error": "name is required"}), 400

    name = data["name"]
    resource_type = data.get("resource_type", "vm")
    sub_type = data.get("sub_type", "")
    ip_address = data.get("ip_address", "")
    port = data.get("port")
    metadata = data.get("metadata", {})
    status = data.get("status", "unknown")

    from utils.auth.stateless_auth import resolve_org_id
    org_id = resolve_org_id(user_id) or ""

    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_onprem_resources
                        (user_id, org_id, name, resource_type, sub_type,
                         ip_address, port, metadata, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (user_id, org_id, name)
                    DO UPDATE SET
                        resource_type = EXCLUDED.resource_type,
                        sub_type = EXCLUDED.sub_type,
                        ip_address = EXCLUDED.ip_address,
                        port = EXCLUDED.port,
                        metadata = EXCLUDED.metadata,
                        status = EXCLUDED.status,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING id;
                    """,
                    (user_id, org_id, name, resource_type, sub_type,
                     ip_address, port, json.dumps(metadata), status),
                )
                row = cur.fetchone()
                conn.commit()
    except Exception as e:
        logger.error("Failed to register on-prem resource: %s", e)
        return jsonify({"error": "Failed to register resource"}), 500

    # Also write to Memgraph as a Service node
    try:
        client = get_memgraph_client()
        client.upsert_service(
            user_id=user_id,
            name=name,
            resource_type=resource_type,
            provider="onprem",
            display_name=name,
            sub_type=sub_type,
            endpoint=f"{ip_address}:{port}" if ip_address and port else ip_address,
            status=status,
            metadata=metadata,
        )
    except Exception as e:
        logger.warning("Failed to write on-prem resource to Memgraph: %s", e)

    return jsonify({"id": row[0] if row else None, "name": name, "status": "created"}), 201


# =========================================================================
# Helper Functions
# =========================================================================

def _normalize_resource(svc):
    """Normalize a Memgraph service dict for API response."""
    name = svc.get("name", "")
    provider = svc.get("provider", "")
    return {
        "id": svc.get("id", f"{provider}:{name}"),
        "name": name,
        "display_name": svc.get("display_name", name),
        "resource_type": svc.get("resource_type", ""),
        "sub_type": svc.get("sub_type", ""),
        "category": get_category(svc.get("resource_type", "")),
        "provider": provider,
        "region": svc.get("region", ""),
        "status": normalize_status(svc.get("status", "")),
        "cloud_resource_id": svc.get("cloud_resource_id", ""),
        "endpoint": svc.get("endpoint", ""),
        "metadata": svc.get("metadata", {}),
        "updated_at": str(svc.get("updated_at", "")),
    }


def _get_onprem_resources(user_id, search=None):
    """Fetch on-prem resources from both user_manual_vms and user_onprem_resources."""
    resources = []
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                if search:
                    cur.execute(
                        """
                        SELECT name, ip_address, port, updated_at FROM user_manual_vms
                        WHERE user_id = %s AND name ILIKE %s
                        UNION ALL
                        SELECT name, ip_address, port, updated_at FROM user_onprem_resources
                        WHERE user_id = %s AND name ILIKE %s
                        ORDER BY name
                        """,
                        (user_id, f"%{search}%", user_id, f"%{search}%"),
                    )
                else:
                    cur.execute(
                        """
                        SELECT name, ip_address, port, updated_at FROM user_manual_vms
                        WHERE user_id = %s
                        UNION ALL
                        SELECT name, ip_address, port, updated_at FROM user_onprem_resources
                        WHERE user_id = %s
                        ORDER BY name
                        """,
                        (user_id, user_id),
                    )
                for row in cur.fetchall():
                    resources.append({
                        "name": row[0],
                        "ip_address": row[1],
                        "port": row[2],
                        "updated_at": row[3],
                    })
    except Exception as e:
        logger.warning("Failed to fetch on-prem resources: %s", e)
    return resources


def _get_related_incidents(user_id, service_name):
    """Fetch incidents related to a service from PostgreSQL."""
    incidents = []
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title, severity, status, created_at
                    FROM incidents
                    WHERE user_id = %s AND service_name = %s
                    ORDER BY created_at DESC
                    LIMIT 10
                    """,
                    (user_id, service_name),
                )
                for row in cur.fetchall():
                    incidents.append({
                        "id": row[0],
                        "title": row[1],
                        "severity": row[2],
                        "status": row[3],
                        "created_at": str(row[4]),
                    })
    except Exception as e:
        logger.debug("Failed to fetch related incidents: %s", e)
    return incidents


def _get_related_alerts(user_id, service_name):
    """Fetch active alerts from monitoring tables that reference this service."""
    alerts = []
    name_lower = service_name.lower()

    alert_sources = [
        {"table": "grafana_alerts", "title_col": "alert_title", "state_col": "alert_state", "source": "grafana"},
        {"table": "datadog_events", "title_col": "event_title", "state_col": "status", "source": "datadog"},
    ]

    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                for src in alert_sources:
                    try:
                        cur.execute(
                            f"""
                            SELECT {src['title_col']}, {src['state_col']}, received_at, %s as source
                            FROM {src['table']}
                            WHERE user_id = %s
                              AND (LOWER({src['title_col']}) LIKE %s OR payload::text ILIKE %s)
                            ORDER BY received_at DESC
                            LIMIT 5
                            """,
                            (src["source"], user_id, f"%{name_lower}%", f"%{name_lower}%"),
                        )
                        for row in cur.fetchall():
                            alerts.append({
                                "title": row[0],
                                "state": row[1],
                                "triggered_at": str(row[2]),
                                "source": row[3],
                            })
                    except Exception as e:
                        logger.debug("Failed to fetch %s alerts: %s", src["source"], e)
    except Exception as e:
        logger.debug("Failed to get DB connection for alerts: %s", e)

    return alerts


def _get_k8s_workloads(user_id, cluster_name):
    """Fetch K8s workloads for a specific cluster from PostgreSQL."""
    workloads = {"pods": [], "deployments": [], "services": []}

    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                # Pods
                cur.execute(
                    """
                    SELECT pod_name, namespace, status FROM k8s_pods
                    WHERE user_id = %s AND cluster_name = %s
                    ORDER BY namespace, pod_name
                    """,
                    (user_id, cluster_name),
                )
                for row in cur.fetchall():
                    workloads["pods"].append({
                        "name": row[0],
                        "namespace": row[1],
                        "status": row[2],
                    })

                # Deployments
                cur.execute(
                    """
                    SELECT deployment_name, namespace, replicas FROM k8s_deployments
                    WHERE user_id = %s AND cluster_name = %s
                    ORDER BY namespace, deployment_name
                    """,
                    (user_id, cluster_name),
                )
                for row in cur.fetchall():
                    workloads["deployments"].append({
                        "name": row[0],
                        "namespace": row[1],
                        "replicas": row[2],
                    })

                # Services
                cur.execute(
                    """
                    SELECT service_name, namespace, type FROM k8s_services
                    WHERE user_id = %s AND cluster_name = %s
                    ORDER BY namespace, service_name
                    """,
                    (user_id, cluster_name),
                )
                for row in cur.fetchall():
                    workloads["services"].append({
                        "name": row[0],
                        "namespace": row[1],
                        "type": row[2],
                    })
    except Exception as e:
        logger.warning("Failed to fetch K8s workloads: %s", e)

    return workloads
