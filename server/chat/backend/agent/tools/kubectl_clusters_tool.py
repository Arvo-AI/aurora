"""
Agent tool: get_connected_clusters
Returns all on-prem Kubernetes clusters the user has connected, with metadata.
The agent uses this to discover which cluster(s) to target before running kubectl.
"""
import json
import logging

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class GetConnectedClustersArgs(BaseModel):
    """No required args -- reads from user context."""
    pass


def get_connected_clusters(**kwargs) -> str:
    """Return connected on-prem Kubernetes clusters with their metadata."""
    user_id = kwargs.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context available"})

    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context, resolve_org_id

        org_id = resolve_org_id(user_id)
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[KubectlClusters:list]")
                cur.execute("""
                    SELECT c.cluster_id, t.cluster_name, c.connected_at, c.last_heartbeat,
                           c.agent_version, c.k8s_context, t.notes, 'agent' AS source
                    FROM active_kubectl_connections c
                    JOIN kubectl_agent_tokens t ON c.token = t.token
                    WHERE (t.user_id = %s OR t.org_id = %s) AND c.status = 'active'

                    UNION ALL

                    SELECT cluster_id, cluster_name, created_at, updated_at,
                           NULL, context_name, NULL, 'kubeconfig' AS source
                    FROM kubeconfig_clusters
                    WHERE org_id = %s AND is_active = TRUE

                    ORDER BY 2
                """, (user_id, org_id, org_id))
                rows = cur.fetchall()

        if not rows:
            return json.dumps({
                "clusters": [],
                "message": "No on-prem Kubernetes clusters connected. Connect a cluster via the connectors page.",
            })

        clusters = []
        for r in rows:
            clusters.append({
                "cluster_id": r[0],
                "name": r[1],
                "connected_at": r[2].isoformat() if r[2] else None,
                "last_heartbeat": r[3].isoformat() if r[3] else None,
                "agent_version": r[4],
                "k8s_context": r[5],
                "notes": r[6],
                "source": r[7],
            })

        return json.dumps({"clusters": clusters})
    except Exception as e:
        logger.exception("Error fetching connected clusters")
        return json.dumps({"error": f"Failed to fetch connected clusters: {e}"})
