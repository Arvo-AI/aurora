"""
Memgraph Client - Sole interface between Aurora and Memgraph graph database.
All Cypher queries are encapsulated here. Uses gqlalchemy driver.
"""

import os
import json
import logging
import threading
from datetime import datetime

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

_client_instance = None
_client_lock = threading.Lock()


def get_memgraph_client():
    """Singleton accessor for the Memgraph client."""
    global _client_instance
    if _client_instance is None:
        with _client_lock:
            if _client_instance is None:
                _client_instance = MemgraphClient(
                    host=os.getenv("MEMGRAPH_HOST", "memgraph"),
                    port=int(os.getenv("MEMGRAPH_PORT", "7687")),
                    username=os.getenv("MEMGRAPH_USER", "aurora"),
                    password=os.getenv("MEMGRAPH_PASSWORD", "aurora_secure_password"),
                )
    return _client_instance


class MemgraphClient:
    """Encapsulates all Memgraph Cypher queries for the Aurora dependency graph."""

    def __init__(self, host, port, username, password):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._driver = None
        self._schema_initialized = False

    def _get_driver(self):
        if self._driver is None:
            uri = f"bolt://{self._host}:{self._port}"
            self._driver = GraphDatabase.driver(
                uri,
                auth=(self._username, self._password),
            )
            logger.info(f"Connected to Memgraph at {uri}")
        if not self._schema_initialized:
            self._schema_initialized = True  # Set BEFORE calling to prevent recursion
            self.ensure_schema()
        return self._driver

    def _execute(self, query, params=None):
        """Execute a Cypher query and return results as list of dicts."""
        driver = self._get_driver()
        try:
            with driver.session() as session:
                result = session.run(query, params or {})
                return [dict(record) for record in result]
        except Exception as e:
            logger.error(f"Memgraph query error: {e}\nQuery: {query}\nParams: {params}")
            raise

    def _execute_no_fetch(self, query, params=None):
        """Execute a Cypher query that returns no results."""
        driver = self._get_driver()
        try:
            with driver.session() as session:
                session.run(query, params or {})
        except Exception as e:
            logger.error(f"Memgraph execute error: {e}\nQuery: {query}")
            raise

    # =========================================================================
    # Schema Initialization
    # =========================================================================

    def ensure_schema(self):
        """Create constraints and indexes if they don't exist."""
        statements = [
            "CREATE CONSTRAINT ON (s:Service) ASSERT s.id IS UNIQUE;",
            "CREATE CONSTRAINT ON (i:Incident) ASSERT i.id IS UNIQUE;",
            "CREATE CONSTRAINT ON (c:Change) ASSERT c.id IS UNIQUE;",
            "CREATE INDEX ON :Service(user_id);",
            "CREATE INDEX ON :Service(name);",
            "CREATE INDEX ON :Service(resource_type);",
            "CREATE INDEX ON :Service(provider);",
            "CREATE INDEX ON :Service(cloud_resource_id);",
            "CREATE INDEX ON :Service(endpoint);",
            "CREATE INDEX ON :Service(vpc_id);",
            "CREATE INDEX ON :Incident(user_id);",
            "CREATE INDEX ON :Incident(postgres_id);",
            "CREATE INDEX ON :Change(user_id);",
        ]
        for stmt in statements:
            try:
                self._execute_no_fetch(stmt)
            except Exception:
                pass  # Constraint/index may already exist
        logger.info("Memgraph schema initialized")

    # =========================================================================
    # Service CRUD
    # =========================================================================

    def upsert_service(self, user_id, name, resource_type, provider, **props):
        """Create or update a Service node."""
        service_id = f"{user_id}:{provider}:{name}"
        query = """
        MERGE (s:Service {id: $id})
        ON CREATE SET
            s.user_id = $user_id,
            s.name = $name,
            s.display_name = $display_name,
            s.resource_type = $resource_type,
            s.sub_type = $sub_type,
            s.provider = $provider,
            s.region = $region,
            s.zone = $zone,
            s.cluster_name = $cluster_name,
            s.namespace = $namespace,
            s.vpc_id = $vpc_id,
            s.cloud_resource_id = $cloud_resource_id,
            s.endpoint = $endpoint,
            s.criticality = $criticality,
            s.team_owner = $team_owner,
            s.metadata = $metadata,
            s.created_at = localDateTime(),
            s.updated_at = localDateTime()
        ON MATCH SET
            s.display_name = $display_name,
            s.resource_type = $resource_type,
            s.sub_type = $sub_type,
            s.region = $region,
            s.zone = $zone,
            s.cluster_name = $cluster_name,
            s.namespace = $namespace,
            s.vpc_id = $vpc_id,
            s.cloud_resource_id = $cloud_resource_id,
            s.endpoint = $endpoint,
            s.criticality = $criticality,
            s.team_owner = $team_owner,
            s.metadata = $metadata,
            s.updated_at = localDateTime()
        RETURN s;
        """
        params = {
            "id": service_id,
            "user_id": user_id,
            "name": name,
            "display_name": props.get("display_name", name),
            "resource_type": resource_type,
            "sub_type": props.get("sub_type", ""),
            "provider": provider,
            "region": props.get("region", ""),
            "zone": props.get("zone", ""),
            "cluster_name": props.get("cluster_name", ""),
            "namespace": props.get("namespace", ""),
            "vpc_id": props.get("vpc_id") or None,
            "cloud_resource_id": props.get("cloud_resource_id", ""),
            "endpoint": props.get("endpoint", ""),
            "criticality": props.get("criticality", "medium"),
            "team_owner": props.get("team_owner", ""),
            "metadata": json.dumps(props.get("metadata", {})) if isinstance(props.get("metadata"), dict) else props.get("metadata", "{}"),
        }
        results = self._execute(query, params)
        return self._node_to_dict(results[0]["s"]) if results else None

    def batch_upsert_services(self, user_id, services):
        """Upsert multiple services in a single transaction. Returns count."""
        count = 0
        for svc in services:
            try:
                self.upsert_service(
                    user_id=user_id,
                    name=svc["name"],
                    resource_type=svc["resource_type"],
                    provider=svc["provider"],
                    **{k: v for k, v in svc.items() if k not in ("name", "resource_type", "provider")},
                )
                count += 1
            except Exception as e:
                logger.warning(f"Failed to upsert service {svc.get('name')}: {e}")
        return count

    def get_service(self, user_id, name):
        """Get a single service by name with its direct dependencies."""
        query = """
        MATCH (s:Service {user_id: $user_id, name: $name})
        OPTIONAL MATCH (s)-[r:DEPENDS_ON]->(upstream:Service)
        OPTIONAL MATCH (downstream:Service)-[r2:DEPENDS_ON]->(s)
        RETURN s,
            collect(DISTINCT {name: upstream.name, type: r.dependency_type, confidence: r.confidence}) AS upstream,
            collect(DISTINCT {name: downstream.name, type: r2.dependency_type, confidence: r2.confidence}) AS downstream;
        """
        results = self._execute(query, {"user_id": user_id, "name": name})
        if not results:
            return None
        row = results[0]
        svc = self._node_to_dict(row["s"])
        svc["upstream"] = [u for u in row.get("upstream", []) if u.get("name")]
        svc["downstream"] = [d for d in row.get("downstream", []) if d.get("name")]
        return svc

    def list_services(self, user_id, resource_type=None, provider=None):
        """List all services for a user, optionally filtered."""
        conditions = ["s.user_id = $user_id"]
        params = {"user_id": user_id}
        if resource_type:
            conditions.append("s.resource_type = $resource_type")
            params["resource_type"] = resource_type
        if provider:
            conditions.append("s.provider = $provider")
            params["provider"] = provider
        where = " AND ".join(conditions)
        query = f"MATCH (s:Service) WHERE {where} RETURN s ORDER BY s.name;"
        results = self._execute(query, params)
        return [self._node_to_dict(r["s"]) for r in results]

    def delete_service(self, user_id, name):
        """Delete a service and all its edges."""
        query = """
        MATCH (s:Service {user_id: $user_id, name: $name})
        DETACH DELETE s
        RETURN count(s) AS deleted;
        """
        results = self._execute(query, {"user_id": user_id, "name": name})
        return results[0]["deleted"] > 0 if results else False

    def find_service_by_endpoint(self, user_id, endpoint):
        """Look up a service by its connection endpoint."""
        query = """
        MATCH (s:Service {user_id: $user_id})
        WHERE s.endpoint CONTAINS $endpoint
        RETURN s LIMIT 1;
        """
        results = self._execute(query, {"user_id": user_id, "endpoint": endpoint})
        return self._node_to_dict(results[0]["s"]) if results else None

    def find_service_by_cloud_id(self, user_id, cloud_resource_id):
        """Look up a service by cloud provider resource ID."""
        query = """
        MATCH (s:Service {user_id: $user_id, cloud_resource_id: $cloud_resource_id})
        RETURN s LIMIT 1;
        """
        results = self._execute(query, {"user_id": user_id, "cloud_resource_id": cloud_resource_id})
        return self._node_to_dict(results[0]["s"]) if results else None

    # =========================================================================
    # Dependency CRUD
    # =========================================================================

    def upsert_dependency(self, user_id, from_service, to_service, dep_type, confidence, discovered_from):
        """Create or update a DEPENDS_ON edge."""
        from_id = self._resolve_service_id(user_id, from_service)
        to_id = self._resolve_service_id(user_id, to_service)
        if not from_id or not to_id:
            return None

        discovered_list = discovered_from if isinstance(discovered_from, list) else [discovered_from]
        query = """
        MATCH (a:Service {id: $from_id})
        MATCH (b:Service {id: $to_id})
        MERGE (a)-[r:DEPENDS_ON]->(b)
        ON CREATE SET
            r.dependency_type = $dep_type,
            r.discovered_from = $discovered_from,
            r.confidence = $confidence,
            r.first_seen = localDateTime(),
            r.last_seen = localDateTime()
        ON MATCH SET
            r.dependency_type = $dep_type,
            r.confidence = CASE
                WHEN $confidence > r.confidence THEN $confidence
                ELSE r.confidence
            END,
            r.last_seen = localDateTime()
        RETURN a.name AS from_name, b.name AS to_name, r.dependency_type AS dep_type, r.confidence AS confidence;
        """
        params = {
            "from_id": from_id,
            "to_id": to_id,
            "dep_type": dep_type,
            "confidence": confidence,
            "discovered_from": discovered_list,
        }
        results = self._execute(query, params)
        return results[0] if results else None

    def batch_upsert_dependencies(self, user_id, deps):
        """Upsert multiple edges. Returns count."""
        count = 0
        for dep in deps:
            try:
                result = self.upsert_dependency(
                    user_id=user_id,
                    from_service=dep["from_service"],
                    to_service=dep["to_service"],
                    dep_type=dep.get("dependency_type", "http"),
                    confidence=dep.get("confidence", 0.5),
                    discovered_from=dep.get("discovered_from", ["unknown"]),
                )
                if result:
                    count += 1
            except Exception as e:
                logger.warning(f"Failed to upsert dependency {dep}: {e}")
        return count

    def get_dependencies(self, user_id, service_name, direction="both"):
        """Get upstream and/or downstream dependencies."""
        result = {"upstream": [], "downstream": []}
        if direction in ("both", "upstream"):
            query = """
            MATCH (s:Service {user_id: $user_id, name: $name})-[r:DEPENDS_ON]->(upstream:Service)
            RETURN upstream.name AS name, r.dependency_type AS dependency_type, r.confidence AS confidence;
            """
            result["upstream"] = self._execute(query, {"user_id": user_id, "name": service_name})
        if direction in ("both", "downstream"):
            query = """
            MATCH (downstream:Service)-[r:DEPENDS_ON]->(s:Service {user_id: $user_id, name: $name})
            RETURN downstream.name AS name, r.dependency_type AS dependency_type, r.confidence AS confidence;
            """
            result["downstream"] = self._execute(query, {"user_id": user_id, "name": service_name})
        return result

    def remove_dependency(self, user_id, from_service, to_service):
        """Remove a specific DEPENDS_ON edge."""
        from_id = self._resolve_service_id(user_id, from_service)
        to_id = self._resolve_service_id(user_id, to_service)
        if not from_id or not to_id:
            return False
        query = """
        MATCH (a:Service {id: $from_id})-[r:DEPENDS_ON]->(b:Service {id: $to_id})
        DELETE r
        RETURN count(r) AS deleted;
        """
        results = self._execute(query, {"from_id": from_id, "to_id": to_id})
        return results[0]["deleted"] > 0 if results else False

    # =========================================================================
    # Graph Traversal
    # =========================================================================

    def get_all_downstream(self, user_id, service_name, max_depth=10):
        """All services that depend on this service (directly or transitively)."""
        service_id = self._resolve_service_id(user_id, service_name)
        if not service_id:
            return []
        query = f"""
        MATCH path = (target:Service {{id: $service_id}})<-[:DEPENDS_ON*1..{max_depth}]-(downstream:Service)
        WHERE downstream.user_id = $user_id
        RETURN DISTINCT downstream.name AS name, downstream.resource_type AS resource_type,
               downstream.provider AS provider, length(path) AS depth
        ORDER BY depth;
        """
        return self._execute(query, {"service_id": service_id, "user_id": user_id})

    def get_all_upstream(self, user_id, service_name, max_depth=10):
        """All services this service depends on (directly or transitively)."""
        service_id = self._resolve_service_id(user_id, service_name)
        if not service_id:
            return []
        query = f"""
        MATCH path = (source:Service {{id: $service_id}})-[:DEPENDS_ON*1..{max_depth}]->(upstream:Service)
        WHERE upstream.user_id = $user_id
        RETURN DISTINCT upstream.name AS name, upstream.resource_type AS resource_type,
               upstream.provider AS provider, length(path) AS depth
        ORDER BY depth;
        """
        return self._execute(query, {"service_id": service_id, "user_id": user_id})

    def are_connected(self, user_id, service_a, service_b):
        """Check if two services have any dependency path between them."""
        id_a = self._resolve_service_id(user_id, service_a)
        id_b = self._resolve_service_id(user_id, service_b)
        if not id_a or not id_b:
            return False
        query = """
        MATCH path = shortestPath(
            (a:Service {id: $id_a})-[:DEPENDS_ON*]-(b:Service {id: $id_b})
        )
        RETURN length(path) AS hops;
        """
        results = self._execute(query, {"id_a": id_a, "id_b": id_b})
        return len(results) > 0

    def get_shortest_path(self, user_id, service_a, service_b):
        """Shortest dependency path between two services."""
        id_a = self._resolve_service_id(user_id, service_a)
        id_b = self._resolve_service_id(user_id, service_b)
        if not id_a or not id_b:
            return []
        query = """
        MATCH path = shortestPath(
            (a:Service {id: $id_a})-[:DEPENDS_ON*]-(b:Service {id: $id_b})
        )
        RETURN [n IN nodes(path) | n.name] AS path_names, length(path) AS hops;
        """
        results = self._execute(query, {"id_a": id_a, "id_b": id_b})
        return results[0] if results else {}

    # =========================================================================
    # Impact Analysis
    # =========================================================================

    def get_impact_radius(self, user_id, service_name):
        """Returns categorized downstream services by depth and criticality."""
        downstream = self.get_all_downstream(user_id, service_name)
        impact = {"critical": [], "high": [], "medium": [], "low": []}
        for svc in downstream:
            depth = svc.get("depth", 1)
            if depth <= 1:
                impact["critical"].append(svc["name"])
            elif depth <= 2:
                impact["high"].append(svc["name"])
            elif depth <= 3:
                impact["medium"].append(svc["name"])
            else:
                impact["low"].append(svc["name"])
        return {
            "service": service_name,
            "impact": impact,
            "total_affected": len(downstream),
        }

    def get_critical_services(self, user_id):
        """Services ranked by PageRank score."""
        try:
            query = """
            CALL pagerank.get()
            YIELD node, rank
            WHERE node:Service AND node.user_id = $user_id
            RETURN node.name AS service, node.resource_type AS type,
                   node.provider AS provider, rank
            ORDER BY rank DESC
            LIMIT 20;
            """
            return self._execute(query, {"user_id": user_id})
        except Exception as e:
            logger.warning(f"PageRank not available: {e}")
            return []

    def get_single_points_of_failure(self, user_id):
        """Services that are graph bridges."""
        try:
            query = """
            CALL bridges.get()
            YIELD node1, node2
            WHERE node1:Service AND node1.user_id = $user_id
            RETURN DISTINCT node1.name AS service, node1.resource_type AS type;
            """
            return self._execute(query, {"user_id": user_id})
        except Exception as e:
            logger.warning(f"Bridges algorithm not available: {e}")
            return []

    # =========================================================================
    # Incident Linking
    # =========================================================================

    def link_incident_to_service(self, user_id, postgres_id, service_name, relationship="AFFECTED", **props):
        """Create Incident node (if needed) and link to Service."""
        incident_id = f"{user_id}:{postgres_id}"
        service_id = self._resolve_service_id(user_id, service_name)
        if not service_id:
            return None
        query = f"""
        MERGE (i:Incident {{id: $incident_id}})
        ON CREATE SET
            i.user_id = $user_id,
            i.postgres_id = $postgres_id,
            i.title = $title,
            i.severity = $severity,
            i.status = $status,
            i.started_at = localDateTime()
        WITH i
        MATCH (s:Service {{id: $service_id}})
        MERGE (i)-[r:{relationship}]->(s)
        ON CREATE SET r.detected_at = localDateTime()
        RETURN i, s;
        """
        params = {
            "incident_id": incident_id,
            "user_id": user_id,
            "postgres_id": postgres_id,
            "title": props.get("title", ""),
            "severity": props.get("severity", "medium"),
            "status": props.get("status", "active"),
            "service_id": service_id,
        }
        results = self._execute(query, params)
        return {"incident_id": incident_id, "service": service_name} if results else None

    def set_root_cause(self, user_id, postgres_id, service_name, confidence, identified_by="rca_agent"):
        """Set CAUSED_BY edge from Incident to Service."""
        incident_id = f"{user_id}:{postgres_id}"
        service_id = self._resolve_service_id(user_id, service_name)
        if not service_id:
            return None
        query = """
        MATCH (i:Incident {id: $incident_id})
        MATCH (s:Service {id: $service_id})
        MERGE (i)-[r:CAUSED_BY]->(s)
        ON CREATE SET
            r.confidence = $confidence,
            r.identified_by = $identified_by,
            r.identified_at = localDateTime()
        ON MATCH SET
            r.confidence = $confidence,
            r.identified_by = $identified_by,
            r.identified_at = localDateTime()
        RETURN i, s;
        """
        params = {
            "incident_id": incident_id,
            "service_id": service_id,
            "confidence": confidence,
            "identified_by": identified_by,
        }
        results = self._execute(query, params)
        return {"incident_id": incident_id, "root_cause": service_name} if results else None

    def get_incident_services(self, user_id, postgres_id):
        """Get all services affected by and causing an incident."""
        incident_id = f"{user_id}:{postgres_id}"
        query = """
        MATCH (i:Incident {id: $incident_id})
        OPTIONAL MATCH (i)-[:AFFECTED]->(affected:Service)
        OPTIONAL MATCH (i)-[cb:CAUSED_BY]->(root:Service)
        RETURN
            collect(DISTINCT affected.name) AS affected_services,
            root.name AS root_cause,
            cb.confidence AS root_cause_confidence;
        """
        results = self._execute(query, {"incident_id": incident_id})
        if not results:
            return None
        row = results[0]
        return {
            "affected_services": row.get("affected_services", []),
            "root_cause": row.get("root_cause"),
            "root_cause_confidence": row.get("root_cause_confidence"),
        }

    # =========================================================================
    # Change Tracking
    # =========================================================================

    def record_change(self, user_id, change_type, service_name, **props):
        """Create a Change node and DEPLOYED_TO edge."""
        import uuid
        change_id = str(uuid.uuid4())
        service_id = self._resolve_service_id(user_id, service_name)
        if not service_id:
            return None
        query = """
        CREATE (c:Change {
            id: $change_id,
            user_id: $user_id,
            change_type: $change_type,
            service_name: $service_name,
            commit_sha: $commit_sha,
            deployed_by: $deployed_by,
            details: $details,
            created_at: localDateTime()
        })
        WITH c
        MATCH (s:Service {id: $service_id})
        CREATE (c)-[:DEPLOYED_TO]->(s)
        RETURN c;
        """
        params = {
            "change_id": change_id,
            "user_id": user_id,
            "change_type": change_type,
            "service_name": service_name,
            "commit_sha": props.get("commit_sha", ""),
            "deployed_by": props.get("deployed_by", ""),
            "details": json.dumps(props.get("details", {})),
            "service_id": service_id,
        }
        self._execute(query, params)
        return {"change_id": change_id, "service": service_name}

    def get_recent_changes(self, user_id, service_name, hours=24):
        """Get changes deployed to a service in the last N hours."""
        service_id = self._resolve_service_id(user_id, service_name)
        if not service_id:
            return []
        query = """
        MATCH (c:Change)-[:DEPLOYED_TO]->(s:Service {id: $service_id})
        WHERE c.created_at > localDateTime() - duration({hours: $hours})
        RETURN c.id AS change_id, c.change_type AS change_type,
               c.commit_sha AS commit_sha, c.deployed_by AS deployed_by,
               c.details AS details, c.created_at AS created_at
        ORDER BY c.created_at DESC;
        """
        return self._execute(query, {"service_id": service_id, "hours": hours})

    def link_incident_to_change(self, user_id, postgres_id, change_id, confidence):
        """Create TRIGGERED_BY edge."""
        incident_id = f"{user_id}:{postgres_id}"
        query = """
        MATCH (i:Incident {id: $incident_id})
        MATCH (c:Change {id: $change_id})
        MERGE (i)-[r:TRIGGERED_BY]->(c)
        ON CREATE SET r.confidence = $confidence, r.time_delta_minutes = 0
        RETURN i, c;
        """
        results = self._execute(query, {
            "incident_id": incident_id,
            "change_id": change_id,
            "confidence": confidence,
        })
        return bool(results)

    # =========================================================================
    # Full Graph Export
    # =========================================================================

    def export_graph(self, user_id):
        """Returns full user graph as {nodes, edges}."""
        nodes_query = """
        MATCH (s:Service {user_id: $user_id})
        RETURN s;
        """
        edges_query = """
        MATCH (a:Service {user_id: $user_id})-[r:DEPENDS_ON]->(b:Service {user_id: $user_id})
        RETURN a.name AS source, b.name AS target,
               r.dependency_type AS dependency_type,
               r.confidence AS confidence,
               r.discovered_from AS discovered_from;
        """
        nodes = [self._node_to_dict(r["s"]) for r in self._execute(nodes_query, {"user_id": user_id})]
        edges = self._execute(edges_query, {"user_id": user_id})
        return {"nodes": nodes, "edges": edges}

    def get_graph_stats(self, user_id):
        """Returns graph statistics."""
        query = """
        MATCH (s:Service {user_id: $user_id})
        WITH count(s) AS total_services,
             collect(s.resource_type) AS types,
             collect(s.provider) AS providers
        OPTIONAL MATCH (:Service {user_id: $user_id})-[r:DEPENDS_ON]->(:Service {user_id: $user_id})
        WITH total_services, types, providers, count(r) AS total_deps
        RETURN total_services, total_deps, types, providers;
        """
        results = self._execute(query, {"user_id": user_id})
        if not results:
            return {"total_services": 0, "total_dependencies": 0}
        row = results[0]
        # Count by type
        type_counts = {}
        for t in row.get("types", []):
            if t:
                type_counts[t] = type_counts.get(t, 0) + 1
        provider_counts = {}
        for p in row.get("providers", []):
            if p:
                provider_counts[p] = provider_counts.get(p, 0) + 1
        return {
            "total_services": row.get("total_services", 0),
            "total_dependencies": row.get("total_deps", 0),
            "services_by_type": type_counts,
            "services_by_provider": provider_counts,
        }

    # =========================================================================
    # Stale Service Management
    # =========================================================================

    def mark_stale_services(self, user_id, stale_days=7):
        """Mark services not updated in N days as stale."""
        query = """
        MATCH (s:Service {user_id: $user_id})
        WHERE s.updated_at < localDateTime() - duration({days: $days})
        SET s.stale = true
        RETURN count(s) AS marked;
        """
        results = self._execute(query, {"user_id": user_id, "days": stale_days})
        return results[0]["marked"] if results else 0

    # =========================================================================
    # Helpers
    # =========================================================================

    def _resolve_service_id(self, user_id, service_name_or_id):
        """Resolve a service name to its full ID. If already an ID, return as-is."""
        if ":" in service_name_or_id and service_name_or_id.count(":") >= 2:
            return service_name_or_id
        # Look up by name
        query = """
        MATCH (s:Service {user_id: $user_id, name: $name})
        RETURN s.id AS id LIMIT 1;
        """
        results = self._execute(query, {"user_id": user_id, "name": service_name_or_id})
        return results[0]["id"] if results else None

    @staticmethod
    def _node_to_dict(node):
        """Convert a neo4j Node to a plain dict."""
        if isinstance(node, dict):
            return node
        try:
            # neo4j driver returns Node objects with items() method
            if hasattr(node, "items"):
                return dict(node.items())
            return dict(node)
        except Exception:
            return {"id": str(node)}
