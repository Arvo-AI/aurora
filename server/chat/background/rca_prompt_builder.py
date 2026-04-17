"""
Shared RCA (Root Cause Analysis) prompt builder for background alert processing.

This module creates provider-aware, persistence-focused RCA prompts that leverage
all available tools and follow the detailed investigation guidelines in the system prompt.

Aurora Learn Integration:
- When Aurora Learn is enabled, searches for similar past incidents with positive feedback
- Injects context from helpful RCAs to improve new investigations
"""

from functools import lru_cache
from typing import Any, Dict, List, Optional
import logging
import os

logger = logging.getLogger(__name__)

RCA_SEGMENTS_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        os.pardir,
        "backend",
        "agent",
        "skills",
        "rca",
        "segments",
    )
)


@lru_cache(maxsize=32)
def _load_rca_segment_template(segment_name: str) -> str:
    """
    Load an RCA markdown segment by name (filename without .md).

    Segment content is cached in-process for performance.
    """
    try:
        from chat.backend.agent.skills.loader import load_core_prompt

        return load_core_prompt(RCA_SEGMENTS_DIR, segments=[segment_name]).strip()
    except Exception as e:
        logger.warning(f"Failed to load RCA segment '{segment_name}': {e}")
        return ""


def _render_rca_segment(segment_name: str, context: Optional[Dict[str, Any]] = None) -> str:
    """Render an RCA segment with optional {variable} template substitutions."""
    template = _load_rca_segment_template(segment_name)
    if not template:
        return ""

    if not context:
        return template

    try:
        from chat.backend.agent.skills.loader import resolve_template

        return resolve_template(template, context)
    except Exception as e:
        logger.warning(f"Failed to render RCA segment '{segment_name}': {e}")
        return template


def _append_rca_segment(
    prompt_parts: List[str],
    segment_name: str,
    context: Optional[Dict[str, Any]] = None,
    leading_blank: bool = False,
    trailing_blank: bool = False,
) -> None:
    """Append rendered segment to prompt_parts with optional surrounding blank lines."""
    content = _render_rca_segment(segment_name, context=context)
    if not content:
        return

    if leading_blank:
        prompt_parts.append("")
    prompt_parts.append(content)
    if trailing_blank:
        prompt_parts.append("")


# ============================================================================
# Aurora Learn - Similar RCA Context Injection
# ============================================================================


def _is_aurora_learn_enabled(user_id: str) -> bool:
    """Check if Aurora Learn is enabled for a user. Defaults to True."""
    if not user_id:
        return False
    try:
        from utils.auth.stateless_auth import get_user_preference
        setting = get_user_preference(user_id, "aurora_learn_enabled", default=True)
        return setting is True
    except Exception as e:
        logger.warning(f"Error checking Aurora Learn setting: {e}")
        return True  # Default to enabled


def inject_aurora_learn_context(
    prompt_parts: list,
    user_id: Optional[str],
    alert_title: str,
    alert_service: str,
    source_type: str,
) -> None:
    """
    Append Aurora Learn context to prompt_parts if similar RCAs are found.

    This is a convenience wrapper for connector modules to inject Aurora Learn
    context into their RCA prompts without duplicating the try/except pattern.

    Args:
        prompt_parts: List of prompt strings to append to (modified in place)
        user_id: User ID for Aurora Learn lookup
        alert_title: Title of the alert
        alert_service: Service associated with the alert
        source_type: Source type (grafana, datadog, etc.)
    """
    if not user_id:
        return

    similar_context = _get_similar_good_rcas_context(
        user_id=user_id,
        alert_title=alert_title,
        alert_service=alert_service,
        source_type=source_type,
    )
    if similar_context:
        prompt_parts.append(similar_context)


def _get_similar_good_rcas_context(
    user_id: str,
    alert_title: str,
    alert_service: str,
    source_type: str,
) -> str:
    """
    Check if Aurora Learn is enabled and search for similar good RCAs.

    Returns formatted context string if matches found, empty string otherwise.
    """
    if not user_id:
        return ""

    # Check if Aurora Learn is enabled
    if not _is_aurora_learn_enabled(user_id):
        logger.debug(f"Aurora Learn disabled for user {user_id}, skipping context injection")
        return ""

    try:
        from routes.incident_feedback.weaviate_client import search_similar_good_rcas

        # Search for similar incidents with positive feedback
        matches = search_similar_good_rcas(
            user_id=user_id,
            alert_title=alert_title,
            alert_service=alert_service,
            source_type=source_type,
            limit=2,
            min_score=0.7,
        )

        if not matches:
            logger.debug(f"No similar good RCAs found for alert: {alert_title[:50]}...")
            return ""

        # Format matches for injection
        context_parts = [
            "",
            "## CONTEXT FROM SIMILAR PAST INCIDENTS:",
            "The following past RCAs were rated helpful by the user. Use this context to guide your investigation:",
            "",
        ]

        for i, match in enumerate(matches, 1):
            similarity_pct = int(match["similarity"] * 100)
            context_parts.extend([
                f"### Past Incident {i} (Similarity: {similarity_pct}%)",
                f"- **Alert**: {match.get('alert_title', 'Unknown')}",
                f"- **Service**: {match.get('alert_service', 'Unknown')}",
                f"- **Source**: {match.get('source_type', 'Unknown')}",
                "",
                "**Summary of what was found:**",
                match.get("aurora_summary", "No summary available")[:1000],  # Limit length
                "",
            ])

            # Add key investigation steps from thoughts (summarized)
            thoughts = match.get("thoughts", [])
            if thoughts:
                # Get the most relevant thoughts (findings and actions)
                key_thoughts = [
                    t["content"]
                    for t in thoughts
                    if t.get("type") in ("finding", "action", "hypothesis", "analysis")
                ][:3]
                if key_thoughts:
                    context_parts.append("**Key investigation steps:**")
                    for thought in key_thoughts:
                        # Truncate long thoughts
                        truncated = thought[:200] + "..." if len(thought) > 200 else thought
                        context_parts.append(f"- {truncated}")
                    context_parts.append("")

            # Add commands used during investigation (without outputs)
            citations = match.get("citations", [])
            if citations:
                commands = [
                    c.get("command", "")
                    for c in citations
                    if c.get("command")
                ][:5]
                if commands:
                    context_parts.append("**Commands used in investigation:**")
                    for cmd in commands:
                        truncated = cmd[:150] + "..." if len(cmd) > 150 else cmd
                        context_parts.append(f"- `{truncated}`")
                    context_parts.append("")

        context_parts.extend([
            "---",
            "**Note**: Use the above context as guidance. The current incident may have different root causes.",
            "",
        ])

        context = "\n".join(context_parts)
        logger.info(
            f"[AURORA LEARN] Injecting context from {len(matches)} similar good RCAs for user {user_id}"
        )
        logger.info(f"[AURORA LEARN] Context preview:\n{context[:500]}...")
        return context

    except Exception as e:
        logger.warning(f"Error getting similar RCA context: {e}")
        return ""


def _get_prediscovery_context(user_id: str, alert_title: str, alert_service: str) -> str:
    """Search prediscovery findings relevant to the alert and return formatted context."""
    if not user_id:
        return ""

    query = " ".join(filter(None, [alert_title, alert_service]))
    if not query.strip():
        return ""

    try:
        from routes.knowledge_base.weaviate_client import _get_weaviate_client
        from weaviate.classes.query import Filter, HybridFusion
        from utils.auth.stateless_auth import get_org_id_for_user

        org_id = get_org_id_for_user(user_id)
        if not org_id:
            return ""

        _, collection = _get_weaviate_client()

        discovery_filter = (
            Filter.by_property("org_id").equal(org_id)
            & Filter.by_property("document_id").like("discovery:*")
        )

        response = collection.query.hybrid(
            query=query,
            limit=3,
            alpha=0.5,
            fusion_type=HybridFusion.RANKED,
            filters=discovery_filter,
            return_metadata=["score"],
        )

        if not response.objects:
            return ""

        parts = [
            "",
            "## INFRASTRUCTURE TOPOLOGY CONTEXT (from pre-discovery):",
            "The following infrastructure mappings were discovered automatically and may be relevant:",
            "",
        ]

        for obj in response.objects:
            source = obj.properties.get("source_filename", "")
            content = obj.properties.get("content", "")
            if content:
                label = source.replace("[Auto-Discovery] ", "") if source else "Discovery"
                parts.append(f"### {label}")
                parts.append(content[:2000])
                parts.append("")

        parts.append("Use this topology context to understand dependencies and blast radius.")
        parts.append("")

        context = "\n".join(parts)
        logger.info(f"[PREDISCOVERY] Injected {len(response.objects)} findings for alert: {query[:50]}")
        return context

    except Exception as e:
        logger.warning(f"Error getting prediscovery context: {e}")
        return ""


def get_user_providers(user_id: str) -> List[str]:
    """Return verified providers for a user.

    Single source of truth: cloud providers (aws/gcp/azure/ovh/scaleway)
    come from user_connections (role-based auth, always valid).
    Integration providers come from SkillRegistry connection checks
    (credential-validated). The agent never sees providers it can't use.
    """
    if not user_id:
        return []

    _cloud_providers = {'aws', 'gcp', 'azure', 'ovh', 'scaleway'}
    verified = []

    try:
        from utils.auth.stateless_auth import get_connected_providers
        all_db = get_connected_providers(user_id)
        verified = [p for p in all_db if p.lower() in _cloud_providers]
    except Exception as e:
        logger.warning(f"Error fetching cloud providers: {e}")

    try:
        from chat.backend.agent.skills.registry import SkillRegistry
        registry = SkillRegistry.get_instance()
        connected_skill_ids = registry.get_connected_skill_ids(user_id)
        verified.extend(connected_skill_ids)
    except Exception as e:
        logger.warning(f"Error fetching connected skills: {e}")

    result = sorted(set(verified))
    logger.info(f"Verified providers for user {user_id}: {result}")
    return result


def _has_onprem_clusters(user_id: Optional[str]) -> bool:
    """Check if user has active on-prem kubectl connections."""
    if not user_id:
        return False
    try:
        from utils.db.db_adapters import connect_to_db_as_user
        conn = connect_to_db_as_user()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM active_kubectl_connections c
                JOIN kubectl_agent_tokens t ON c.token = t.token
                WHERE t.user_id = %s AND c.status = 'active'
            """, (user_id,))
            count = cursor.fetchone()[0]
            cursor.close()
            return count > 0
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Error checking on-prem clusters: {e}")
        return False


def _build_provider_investigation_section(providers: List[str], user_id: Optional[str] = None) -> str:
    """Provider investigation now loaded from skills/rca/ files."""
    return ""

def _get_github_connected(user_id: str) -> bool:
    """Check if user has GitHub connected."""
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        creds = get_credentials_from_db(user_id, "github")
        return bool(creds and creds.get("access_token"))
    except Exception as e:
        logger.warning(f"Error checking GitHub connection for user {user_id}: {e}")
        return False


def _has_jenkins_connected(user_id: str) -> bool:
    """Check if user has Jenkins connected."""
    try:
        from utils.auth.token_management import get_token_data
        creds = get_token_data(user_id, "jenkins")
        return bool(creds and creds.get("base_url"))
    except Exception as e:
        logger.warning(f"Error checking Jenkins context: {e}")
        return False


def _has_cloudbees_connected(user_id: str) -> bool:
    """Check if user has CloudBees CI connected."""
    try:
        from utils.auth.token_management import get_token_data
        creds = get_token_data(user_id, "cloudbees")
        return bool(creds and creds.get("base_url"))
    except Exception as e:
        logger.warning(f"Error checking CloudBees context: {e}")
        return False


def _has_jira_connected(user_id: str) -> bool:
    """Check if user has Jira connected and the feature flag is enabled."""
    try:
        from utils.flags.feature_flags import is_jira_enabled
        if not is_jira_enabled():
            return False
        from utils.auth.token_management import get_token_data
        creds = get_token_data(user_id, "jira")
        return bool(creds and (creds.get("access_token") or creds.get("pat_token")))
    except Exception as e:
        logger.warning(f"Error checking Jira context: {e}")
        return False


def _has_confluence_connected(user_id: str) -> bool:
    """Check if user has Confluence connected and the feature flag is enabled."""
    try:
        from utils.flags.feature_flags import is_confluence_enabled
        if not is_confluence_enabled():
            return False
        from utils.auth.token_management import get_token_data
        creds = get_token_data(user_id, "confluence")
        return bool(creds and (creds.get("access_token") or creds.get("pat_token")))
    except Exception as e:
        logger.warning(f"Error checking Confluence context: {e}")
        return False


def _get_recent_jenkins_deployments(user_id: str, service: str = "", lookback_minutes: int = 60, provider: str = "") -> List[Dict[str, Any]]:
    """Query jenkins_deployment_events for recent deployments matching a service.

    Used to inject deployment context into ANY RCA prompt (not just Jenkins-sourced).
    """
    if not user_id:
        return []
    lookback_minutes = max(1, min(int(lookback_minutes), 10080))  # 1 min to 7 days
    try:
        from utils.db.connection_pool import db_pool
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                conditions = ["user_id = %s", "received_at >= NOW() - make_interval(mins => %s)"]
                params: list = [user_id, lookback_minutes]

                if service and service != "unknown":
                    conditions.append("service = %s")
                    params.append(service)

                if provider:
                    conditions.append("provider = %s")
                    params.append(provider)

                where = " AND ".join(conditions)
                cursor.execute(
                    f"""SELECT service, environment, result, build_number, build_url,
                              commit_sha, branch, deployer, trace_id, received_at
                       FROM jenkins_deployment_events
                       WHERE {where}
                       ORDER BY received_at DESC LIMIT 5""",
                    tuple(params),
                )
                rows = cursor.fetchall()
                return [
                    {
                        "service": r[0], "environment": r[1], "result": r[2],
                        "build_number": r[3], "build_url": r[4], "commit_sha": r[5] or "",
                        "branch": r[6], "deployer": r[7], "trace_id": r[8],
                        "webhook_received_at": r[9].isoformat() if r[9] else None,
                    }
                    for r in rows
                ]
    except Exception as e:
        logger.warning(f"Error fetching recent Jenkins deployments: {e}")
        return []


def build_rca_prompt(
    source: str,
    alert_details: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    integrations: Optional[Dict[str, bool]] = None,
) -> str:
    """Build a comprehensive, provider-aware RCA prompt."""
    # Fetch providers if not provided
    if not providers and user_id:
        providers = get_user_providers(user_id)

    providers = providers or []
    providers_lower = [p.lower() for p in providers]

    # Derive integrations from skill registry when not passed by caller
    if integrations is None and user_id:
        try:
            from chat.backend.agent.skills.registry import SkillRegistry
            registry = SkillRegistry.get_instance()
            connected_ids = registry.get_connected_skill_ids(user_id)
            integrations = {sid: True for sid in connected_ids}
        except Exception:
            integrations = {}

    # Extract alert service name early — used by multiple sections below.
    # Start with the explicit service label, then try richer fallbacks from
    # the alert message (Condition/Policy/Targets fields) so downstream
    # consumers (Jenkins deploy lookup, Aurora Learn, prediscovery) get the
    # same specificity as the Jira search term.
    _label_service = alert_details.get('labels', {}).get('service', '')
    alert_service = _label_service if _label_service and _label_service != 'unknown' else ''
    if not alert_service and source == 'netdata':
        alert_service = alert_details.get('host', '') or ''

    # Format alert details
    title = alert_details.get('title', 'Unknown Alert')
    status = alert_details.get('status', 'unknown')
    labels = alert_details.get('labels', {})
    message = alert_details.get('message', '')
    values = alert_details.get('values', '')

    # If alert_service is still empty, try to extract a meaningful service/component
    # name from the message (same heuristic the Jira search uses).
    _generic_titles = {
        'new relic alert', 'unknown alert', 'alert', 'unknown',
        'grafana alert', 'datadog alert', 'splunk alert',
    }
    if not alert_service:
        _candidate = title if title.lower().strip() not in _generic_titles else ''
        if not _candidate:
            _msg = message or ''
            for _part in _msg.replace('.', ',').split(','):
                _part = _part.strip()
                for _prefix in ('Condition:', 'Targets:', 'Entities:', 'Policy:', 'Search:'):
                    if _part.startswith(_prefix):
                        _candidate = _part[len(_prefix):].strip()
                        break
                if _candidate:
                    break
        if _candidate:
            alert_service = _candidate

    # Source-specific labels formatting
    if source == 'grafana':
        labels_str = ", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "none"
    elif source == 'datadog':
        tags = alert_details.get('tags', [])
        labels_str = ", ".join(tags[:10]) if tags else "none"
    elif source == 'netdata':
        host = alert_details.get('host', 'unknown')
        chart = alert_details.get('chart', 'unknown')
        labels_str = f"host={host}, chart={chart}"
    elif source == 'pagerduty':
        labels_str = ", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "none"
    elif source == 'splunk':
        labels_str = ", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "none"
    elif source == 'dynatrace':
        entity = alert_details.get('impacted_entity', 'unknown')
        impact = alert_details.get('impact', 'unknown')
        labels_str = f"entity={entity}, impact={impact}"
    elif source == 'bigpanda':
        labels_str = ", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "none"
    elif source == 'newrelic':
        labels_str = ", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "none"
    elif source == 'chat':
        labels_str = ", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "user-reported"
    elif source == 'opsgenie':
        tags = alert_details.get('tags', [])
        labels_str = ", ".join(tags[:10]) if tags else "none"
    else:
        labels_str = str(labels)

    # Build the prompt
    prompt_parts = [
        f"# ROOT CAUSE ANALYSIS REQUIRED - {source.upper()} ALERT",
        "",
        "## ALERT DETAILS:",
        f"- **Title**: {title}",
        f"- **Status**: {status}",
        f"- **Source**: {source}",
        f"- **Labels/Tags**: {labels_str}",
    ]

    if message:
        prompt_parts.append(f"- **Message**: {message}")
    if values:
        prompt_parts.append(f"- **Values**: {values}")
    if source == 'datadog' and 'monitor_id' in alert_details:
        prompt_parts.append(f"- **Monitor ID**: {alert_details['monitor_id']}")
    if source == 'pagerduty':
        if 'incident_id' in alert_details:
            prompt_parts.append(f"- **Incident ID**: {alert_details['incident_id']}")
        if 'incident_url' in alert_details:
            prompt_parts.append(f"- **Incident URL**: {alert_details['incident_url']}")
    if source == 'netdata':
        prompt_parts.append(f"- **Host**: {alert_details.get('host', 'unknown')}")
        prompt_parts.append(f"- **Chart**: {alert_details.get('chart', 'unknown')}")
    if source == 'newrelic':
        if 'issueUrl' in alert_details:
            prompt_parts.append(f"- **Issue URL**: {alert_details['issueUrl']}")

    # providers list is already verified by get_user_providers() — only
    # cloud providers with valid role-auth + SkillRegistry-validated integrations.
    prompt_parts.extend([
        "",
        "## CONNECTED INFRASTRUCTURE & MONITORING:",
        f"You have access to: {', '.join(providers) if providers else 'No cloud/monitoring providers connected'}",
    ])

    # All integration guidance (GitHub, Jira, Confluence, Jenkins, CloudBees,
    # provider investigation commands) loaded from skill files via SkillRegistry
    # in the system prompt (background.py). No skill loading here — the user
    # message should contain only alert details and investigation context.

    # Aurora Learn: Inject context from similar past incidents
    if user_id:
        similar_context = _get_similar_good_rcas_context(
            user_id=user_id,
            alert_title=title,
            alert_service=alert_service,
            source_type=source,
        )
        if similar_context:
            prompt_parts.append(similar_context)

    # Prediscovery: Inject infrastructure topology context
    if user_id:
        prediscovery_context = _get_prediscovery_context(
            user_id=user_id,
            alert_title=title,
            alert_service=alert_service,
        )
        if prediscovery_context:
            prompt_parts.append(prediscovery_context)

    # Critical investigation requirements (modular markdown segments)
    _append_rca_segment(
        prompt_parts,
        "critical_requirements_header",
        leading_blank=True,
        trailing_blank=True,
    )

    has_infra_providers = bool({'gcp', 'aws', 'azure', 'ovh', 'scaleway'}.intersection(set(providers_lower)))
    has_jira = bool((integrations or {}).get('jira'))
    has_confluence = bool((integrations or {}).get('confluence'))
    after_context_label = 'Jira' if has_jira else 'Confluence' if has_confluence else 'change'
    
    # Add aggressive persistence prompts only if cost optimization is disabled
    # The immediate action required due to the AgentExecutor which assumes agent is done when it sends a text chunk without a tool call.
    if os.getenv("RCA_OPTIMIZE_COSTS", "").lower() != "true":
        _append_rca_segment(
            prompt_parts,
            "persistence_and_immediate_action",
            context={"after_context_label": after_context_label},
            trailing_blank=True,
        )
    
    depth_steps = []
    if has_jira or has_confluence:
        depth_steps.append("**Search Jira/Confluence first** for recent changes, open bugs, and runbooks")
    depth_steps.extend([
        "Start broad - understand the overall system state",
        "Identify the affected component(s)",
        "Drill down into specifics - logs, metrics, configurations",
        "Check related/dependent resources",
        "Look for recent changes that correlate with the issue",
    ])
    if has_infra_providers:
        depth_steps.extend([
            "Compare with healthy resources of the same type",
            "Check resource quotas, limits, and constraints",
            "Examine network connectivity and security rules",
            "Verify IAM permissions and service accounts",
        ])
    depth_steps.append("Review historical patterns if available")
    prompt_parts.append("### INVESTIGATION DEPTH:")
    for i, step in enumerate(depth_steps, 1):
        prompt_parts.append(f"{i}. {step}")

    _append_rca_segment(prompt_parts, "error_resilience_intro", leading_blank=True)
    if has_infra_providers:
        _append_rca_segment(prompt_parts, "error_resilience_infra")
    _append_rca_segment(prompt_parts, "error_resilience_outro")

    _append_rca_segment(prompt_parts, "what_to_investigate", leading_blank=True)
    _append_rca_segment(prompt_parts, "output_requirements", leading_blank=True)

    return "\n".join(prompt_parts)


def build_grafana_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from Grafana alert payload."""
    title = payload.get("title") or payload.get("ruleName") or "Unknown Alert"
    status = payload.get("state") or payload.get("status") or "unknown"
    message = payload.get("message") or payload.get("annotations", {}).get("description") or ""
    labels = payload.get("commonLabels", {}) or payload.get("labels", {})

    values = payload.get("values") or payload.get("evalMatches", [])
    values_str = ""
    if values:
        if isinstance(values, list):
            values_str = ", ".join(str(v) for v in values[:5])
        elif isinstance(values, dict):
            values_str = ", ".join(f"{k}: {v}" for k, v in list(values.items())[:5])

    alert_details = {
        'title': title,
        'status': status,
        'message': message,
        'labels': labels,
        'values': values_str,
    }

    return build_rca_prompt('grafana', alert_details, providers, user_id)


def build_datadog_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from Datadog alert payload."""
    title = payload.get("title") or payload.get("event_title") or payload.get("event", {}).get("title") or "Unknown Alert"
    status = payload.get("status") or payload.get("state") or payload.get("alert_type") or "unknown"
    event_type = payload.get("event_type") or payload.get("alert_type") or "unknown"
    scope = payload.get("scope") or payload.get("event", {}).get("scope") or "none"
    tags = payload.get("tags", [])
    monitor_id = payload.get("monitor_id") or payload.get("alert_id") or "unknown"
    message = payload.get("body") or payload.get("message") or payload.get("event", {}).get("text") or ""

    alert_details = {
        'title': title,
        'status': f"{status} ({event_type})",
        'message': message,
        'tags': tags,
        'monitor_id': monitor_id,
        'scope': scope,
    }

    return build_rca_prompt('datadog', alert_details, providers, user_id)


def build_dynatrace_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from Dynatrace problem notification payload."""
    title = payload.get("ProblemTitle") or "Unknown Problem"
    impact = payload.get("ProblemImpact") or "unknown"
    entity = payload.get("ImpactedEntity") or "unknown"
    problem_url = payload.get("ProblemURL") or ""
    tags = payload.get("Tags") or ""

    alert_details = {
        'title': title,
        'status': payload.get("State", "OPEN"),
        'message': f"Impact: {impact}. Entity: {entity}",
        'labels': {},
        'impacted_entity': entity,
        'impact': impact,
    }
    if problem_url:
        alert_details['problemUrl'] = problem_url
    if tags:
        alert_details['tags'] = tags

    return build_rca_prompt('dynatrace', alert_details, providers, user_id)


def build_netdata_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from Netdata alert payload."""
    alarm = payload.get("name") or payload.get("alarm") or payload.get("title") or "Unknown Alert"
    status = payload.get("status") or "unknown"
    host = payload.get("host") or "unknown"
    chart = payload.get("chart") or "unknown"
    alert_class = payload.get("class") or "unknown"
    family = payload.get("family") or "unknown"
    space = payload.get("space") or "unknown"
    room = payload.get("room") or "unknown"
    value = payload.get("value")
    message = payload.get("message") or payload.get("info") or ""

    values_str = str(value) if value is not None else ""

    alert_details = {
        'title': alarm,
        'status': status,
        'message': message,
        'host': host,
        'chart': chart,
        'labels': {
            'class': alert_class,
            'family': family,
            'space': space,
            'room': room,
        },
        'values': values_str,
    }

    return build_rca_prompt('netdata', alert_details, providers, user_id)


def build_pagerduty_rca_prompt(
    incident: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from PagerDuty V3 incident data."""
    title = incident.get("title", "Untitled Incident")
    incident_number = incident.get("number", "unknown")
    incident_id = incident.get("id", "unknown")
    status = incident.get("status", "unknown")
    urgency = incident.get("urgency", "unknown")
    
    # Service information
    service = incident.get("service", {})
    service_name = service.get("summary", "unknown") if isinstance(service, dict) else "unknown"
    
    # Priority information
    priority = incident.get("priority", {})
    priority_name = priority.get("summary") or priority.get("name", "none") if isinstance(priority, dict) else "none"
    
    # Description
    description = incident.get("body", {}).get("details", "")
    
    # HTML URL
    html_url = incident.get("html_url", "")
    
    # Incident key
    incident_key = incident.get("incident_key", "")
    
    # Build alert details for the unified prompt builder
    alert_details = {
        'title': f"#{incident_number}: {title}",
        'status': f"{status} (urgency: {urgency})",
        'message': description,
        'labels': {
            'incident_id': incident_id,
            'incident_number': str(incident_number),
            'urgency': urgency,
            'priority': priority_name,
            'service': service_name,
        },
        'incident_url': html_url,
        'incident_id': incident_id,
    }
    
    if incident_key:
        alert_details['labels']['incident_key'] = incident_key
    
    # Add escalation policy
    if escalation_policy := incident.get("escalation_policy", {}):
        if isinstance(escalation_policy, dict):
            ep_name = escalation_policy.get("summary") or escalation_policy.get("name", "")
            if ep_name:
                alert_details['labels']['escalation_policy'] = ep_name
    
    # Add assignments
    if assignments := incident.get("assignments", []):
        if isinstance(assignments, list) and assignments:
            assignees = []
            for assignment in assignments[:3]:
                if isinstance(assignment, dict):
                    assignee = assignment.get("assignee", {})
                    if isinstance(assignee, dict):
                        assignee_name = assignee.get("summary") or assignee.get("name", "")
                        if assignee_name:
                            assignees.append(assignee_name)
            if assignees:
                alert_details['labels']['assigned_to'] = ', '.join(assignees)
    
    # Add teams
    if teams := incident.get("teams", []):
        if isinstance(teams, list) and teams:
            team_names = []
            for team in teams[:3]:
                if isinstance(team, dict):
                    team_name = team.get("summary") or team.get("name", "")
                    if team_name:
                        team_names.append(team_name)
            if team_names:
                alert_details['labels']['teams'] = ', '.join(team_names)
    
    # Add custom fields
    if custom_fields := incident.get("customFields", {}):
        if isinstance(custom_fields, dict) and custom_fields:
            for field_name, field_value in custom_fields.items():
                alert_details['labels'][f"custom_{field_name}"] = str(field_value)
    
    return build_rca_prompt('pagerduty', alert_details, providers, user_id)


def build_jenkins_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from a Jenkins deployment failure event."""
    service = payload.get("service") or payload.get("job_name") or "Unknown Service"
    result = payload.get("result", "FAILURE")
    environment = payload.get("environment", "unknown")
    git = payload.get("git", {})

    alert_details = {
        'title': f"Jenkins Deployment {result}: {service}",
        'status': result,
        'message': f"Build #{payload.get('build_number', '?')} deployed to {environment}",
        'labels': {
            'service': service,
            'environment': environment,
            'deployer': payload.get('deployer', ''),
        },
    }

    if git.get("commit_sha"):
        alert_details['labels']['commit'] = git['commit_sha']
    if git.get("branch"):
        alert_details['labels']['branch'] = git['branch']
    if payload.get("trace_id"):
        alert_details['labels']['trace_id'] = payload['trace_id']

    return build_rca_prompt('jenkins', alert_details, providers, user_id)


def build_cloudbees_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from a CloudBees CI deployment failure event."""
    service = payload.get("service") or payload.get("job_name") or "Unknown Service"
    result = payload.get("result", "FAILURE")
    environment = payload.get("environment", "unknown")
    git = payload.get("git", {})

    alert_details = {
        'title': f"CloudBees CI Deployment {result}: {service}",
        'status': result,
        'message': f"Build #{payload.get('build_number', '?')} deployed to {environment}",
        'labels': {
            'service': service,
            'environment': environment,
            'deployer': payload.get('deployer', ''),
        },
    }

    if git.get("commit_sha"):
        alert_details['labels']['commit'] = git['commit_sha']
    if git.get("branch"):
        alert_details['labels']['branch'] = git['branch']
    if payload.get("trace_id"):
        alert_details['labels']['trace_id'] = payload['trace_id']

    return build_rca_prompt('cloudbees', alert_details, providers, user_id)


def build_spinnaker_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from a Spinnaker pipeline failure event."""
    application = payload.get("application") or "Unknown Application"
    pipeline_name = payload.get("pipeline_name") or payload.get("pipeline", "Unknown Pipeline")
    status = payload.get("status", "TERMINAL")
    trigger_type = payload.get("trigger_type", "unknown")
    trigger_user = payload.get("trigger_user", "unknown")

    alert_details = {
        'title': f"Spinnaker Pipeline {status}: {application}/{pipeline_name}",
        'status': status,
        'message': f"Pipeline '{pipeline_name}' for application '{application}' ended with status {status}",
        'labels': {
            'service': application,
            'pipeline': pipeline_name,
            'trigger_type': trigger_type,
            'trigger_user': trigger_user,
        },
    }

    execution_id = payload.get("execution_id")
    if execution_id:
        alert_details['labels']['execution_id'] = execution_id

    return build_rca_prompt('spinnaker', alert_details, providers, user_id)


def build_bigpanda_rca_prompt(
    incident: Dict[str, Any],
    alerts: list,
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from BigPanda incident payload."""
    first_alert = alerts[0] if alerts else {}
    title = (
        first_alert.get("description")
        or first_alert.get("condition_name")
        or f"BigPanda Incident {incident.get('id', 'unknown')}"
    )
    service = str(
        first_alert.get("primary_property")
        or first_alert.get("source_system")
        or "unknown"
    )
    bp_status = incident.get("status", "active")

    message_parts = [f"Child alerts: {len(alerts)}"]
    if envs := incident.get("environments"):
        message_parts.append(f"Environments: {envs}")
    if tags := incident.get("incident_tags"):
        message_parts.append(f"Tags: {tags}")
    if alerts:
        summaries = []
        for a in alerts[:5]:
            desc = a.get("description") or a.get("condition_name") or "no description"
            src = a.get("source_system") or "unknown"
            summaries.append(f"[{src}] {desc}")
        message_parts.append("Top alerts: " + "; ".join(summaries))

    alert_details = {
        'title': title,
        'status': bp_status,
        'message': ". ".join(message_parts),
        'labels': {
            'service': service,
            'severity': incident.get("severity", "unknown"),
            'child_alert_count': str(len(alerts)),
        },
    }

    return build_rca_prompt('bigpanda', alert_details, providers, user_id)


def build_splunk_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from Splunk alert payload."""
    search_name = payload.get("search_name") or payload.get("name") or "Unknown Alert"
    result_count = payload.get("result_count") or payload.get("results_count") or 0
    search_query = payload.get("search") or payload.get("search_query") or ""
    app = payload.get("app") or payload.get("source") or ""
    severity = payload.get("severity") or payload.get("alert_severity") or ""

    results = payload.get("results") or payload.get("result") or []
    results_str = ""
    if results:
        if isinstance(results, list):
            results_str = ", ".join(str(r) for r in results[:5])
        elif isinstance(results, dict):
            results_str = str(results)

    message_parts = [f"Search: {search_name}", f"Result count: {result_count}"]
    if search_query:
        message_parts.append(f"SPL: {search_query}")
    if results_str:
        message_parts.append(f"Sample: {results_str}")

    alert_details = {
        'title': search_name,
        'status': f"triggered ({result_count} results)",
        'message': ". ".join(message_parts),
        'labels': {},
    }

    if app:
        alert_details['labels']['app'] = app
    if severity:
        alert_details['labels']['severity'] = str(severity)

    return build_rca_prompt('splunk', alert_details, providers, user_id)


def build_newrelic_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from New Relic alert/issue webhook payload."""
    from routes.newrelic.tasks import extract_newrelic_title
    title = extract_newrelic_title(payload)
    state = payload.get("state") or payload.get("currentState") or payload.get("current_state") or "unknown"
    priority = payload.get("priority") or payload.get("severity") or "unknown"
    condition_name = payload.get("conditionName") or payload.get("condition_name") or ""
    policy_name = payload.get("policyName") or payload.get("policy_name") or ""
    issue_url = payload.get("issueUrl") or payload.get("violationChartUrl") or payload.get("incident_url") or ""
    account_id = payload.get("accountId") or payload.get("account_id") or ""

    entities = payload.get("entitiesData", {}).get("entities", [])
    entity_names = [e.get("name", "unknown") for e in entities[:5]] if entities else []
    targets = payload.get("targets", [])
    target_names = [t.get("name", "unknown") for t in targets[:5]] if targets else []

    details = payload.get("details") or ""

    message_parts = []
    if condition_name:
        message_parts.append(f"Condition: {condition_name}")
    if policy_name:
        message_parts.append(f"Policy: {policy_name}")
    if entity_names:
        message_parts.append(f"Entities: {', '.join(entity_names)}")
    elif target_names:
        message_parts.append(f"Targets: {', '.join(target_names)}")
    if payload.get("totalIncidents"):
        message_parts.append(f"Total incidents: {payload['totalIncidents']}")
    if details:
        message_parts.append(f"Details: {details[:500]}")

    labels: Dict[str, str] = {}
    if priority and priority != "unknown":
        labels["priority"] = priority
    if account_id:
        labels["accountId"] = str(account_id)

    alert_details = {
        'title': title,
        'status': f"{state} (priority: {priority})",
        'message': ". ".join(message_parts) if message_parts else title,
        'labels': labels,
    }
    if issue_url:
        alert_details['issueUrl'] = issue_url

    return build_rca_prompt('newrelic', alert_details, providers, user_id)


def build_chat_rca_prompt(
    description: str,
    title: str = "",
    service: str = "",
    severity: str = "medium",
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from a user-reported incident in chat.

    Wraps the user's free-text description into the standard alert_details
    format and delegates to the shared build_rca_prompt().
    """
    alert_title = title or f"User-reported: {description[:80]}"

    labels: Dict[str, str] = {}
    if service:
        labels["service"] = service
    if severity:
        labels["severity"] = severity

    alert_details = {
        "title": alert_title,
        "status": "investigating",
        "message": description,
        "labels": labels,
    }

    return build_rca_prompt("chat", alert_details, providers, user_id)


def build_opsgenie_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from OpsGenie alert webhook payload."""
    alert = payload.get("alert", {})
    message = alert.get("message") or "Unknown Alert"
    action = payload.get("action") or "unknown"
    priority = alert.get("priority") or "unknown"
    status = alert.get("status") or "unknown"
    source = alert.get("source") or "unknown"
    description = alert.get("description") or ""
    entity = alert.get("entity") or ""
    tags = alert.get("tags", [])
    teams = alert.get("teams", [])

    message_parts = []
    if description:
        message_parts.append(description)
    if entity:
        message_parts.append(f"Entity: {entity}")
    if teams:
        message_parts.append(f"Teams: {', '.join(teams) if isinstance(teams, list) else str(teams)}")

    alert_details = {
        'title': message,
        'status': f"{status} (action: {action}, priority: {priority})",
        'message': ". ".join(message_parts) if message_parts else message,
        'tags': tags,
        'source': source,
    }
    if entity:
        alert_details['entity'] = entity

    return build_rca_prompt('opsgenie', alert_details, providers, user_id)
