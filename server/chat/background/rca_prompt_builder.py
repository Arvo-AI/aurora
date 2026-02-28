"""
Shared RCA (Root Cause Analysis) prompt builder for background alert processing.

This module creates provider-aware, persistence-focused RCA prompts that leverage
all available tools and follow the detailed investigation guidelines in the system prompt.

Aurora Learn Integration:
- When Aurora Learn is enabled, searches for similar past incidents with positive feedback
- Injects context from helpful RCAs to improve new investigations
"""

from typing import Any, Dict, List, Optional
import logging
import os

logger = logging.getLogger(__name__)


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


def get_user_providers(user_id: str) -> List[str]:
    """Fetch connected cloud providers for a user from the database."""
    if not user_id:
        return []

    try:
        from utils.auth.stateless_auth import get_connected_providers
        providers = get_connected_providers(user_id)
        if providers:
            logger.info(f"Fetched connected providers for RCA: {providers}")
            return providers
        logger.info(f"No connected providers found for user {user_id}")

        return []
    except Exception as e:
        logger.warning(f"Error fetching connected providers for RCA: {e}")
        return []


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
    """Build provider-specific investigation instructions."""
    sections = []
    providers_lower = [p.lower() for p in providers] if providers else []

    # On-Prem Kubernetes-specific instructions
    if _has_onprem_clusters(user_id):
        sections.append("""
## On-Premise Kubernetes Investigation:
- Available clusters are listed in the "ON-PREM KUBERNETES CLUSTERS" section above
- Get pod details: on_prem_kubectl('get pods -n NAMESPACE -o wide', 'CLUSTER_ID')
- Describe pods: on_prem_kubectl('describe pod POD_NAME -n NAMESPACE', 'CLUSTER_ID')
- Check pod logs: on_prem_kubectl('logs POD_NAME -n NAMESPACE --since=1h --tail=200', 'CLUSTER_ID')
- Check events: on_prem_kubectl('get events -n NAMESPACE --sort-by=.lastTimestamp', 'CLUSTER_ID')
- Check node health: on_prem_kubectl('describe node NODE_NAME', 'CLUSTER_ID')
- Check deployments: on_prem_kubectl('get deployments -n NAMESPACE', 'CLUSTER_ID')
- Check all pods: on_prem_kubectl('get pods -A', 'CLUSTER_ID')
- **CRITICAL**: Use on_prem_kubectl tool with cluster_id from list above, NOT terminal_exec or cloud_exec""")

    # GCP-specific instructions
    if 'gcp' in providers_lower:
        sections.append("""
## GCP/GKE Investigation:
- Check cluster status: cloud_tool('gcp', 'container clusters list')
- **IMPORTANT**: Get cluster credentials first: cloud_tool('gcp', 'container clusters get-credentials CLUSTER_NAME --region=REGION')
- Get pod details: cloud_tool('gcp', 'kubectl get pods -n NAMESPACE -o wide')
- Describe problematic pods: cloud_tool('gcp', 'kubectl describe pod POD_NAME -n NAMESPACE')
- Check pod logs: cloud_tool('gcp', 'kubectl logs POD_NAME -n NAMESPACE --since=1h')
- Check pod metrics: cloud_tool('gcp', 'kubectl top pod POD_NAME -n NAMESPACE')
- Check events: cloud_tool('gcp', 'kubectl get events -n NAMESPACE --sort-by=.lastTimestamp')
- Check node health: cloud_tool('gcp', 'kubectl describe node NODE_NAME')
- Query Stackdriver logs: cloud_tool('gcp', 'logging read "resource.type=k8s_container" --limit=50 --freshness=1h')
- Check deployments: cloud_tool('gcp', 'kubectl get deployments -n NAMESPACE')
- Check services: cloud_tool('gcp', 'kubectl get svc -n NAMESPACE')
- Check HPA: cloud_tool('gcp', 'kubectl get hpa -n NAMESPACE')
- Check PVC status: cloud_tool('gcp', 'kubectl get pvc -n NAMESPACE')""")

    # AWS-specific instructions
    if 'aws' in providers_lower:
        sections.append("""
## AWS/EKS Investigation:
- Check cluster status: cloud_tool('aws', 'eks describe-cluster --name CLUSTER_NAME')
- **IMPORTANT**: Get cluster credentials first: cloud_tool('aws', 'eks update-kubeconfig --name CLUSTER_NAME --region REGION')
- Get pod details: cloud_tool('aws', 'kubectl get pods -n NAMESPACE -o wide')
- Describe problematic pods: cloud_tool('aws', 'kubectl describe pod POD_NAME -n NAMESPACE')
- Check pod logs: cloud_tool('aws', 'kubectl logs POD_NAME -n NAMESPACE --since=1h')
- Check pod metrics: cloud_tool('aws', 'kubectl top pod POD_NAME -n NAMESPACE')
- Check events: cloud_tool('aws', 'kubectl get events -n NAMESPACE --sort-by=.lastTimestamp')
- Check node health: cloud_tool('aws', 'kubectl describe node NODE_NAME')
- Query CloudWatch logs: cloud_tool('aws', 'logs filter-log-events --log-group-name LOG_GROUP --start-time TIMESTAMP')
- Check EC2 instances: cloud_tool('aws', 'ec2 describe-instances --filters "Name=tag:Name,Values=*"')
- Check CloudWatch metrics: cloud_tool('aws', 'cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name CPUUtilization')
- Check load balancers: cloud_tool('aws', 'elbv2 describe-load-balancers')
- Check security groups: cloud_tool('aws', 'ec2 describe-security-groups')""")

    # Azure-specific instructions
    if 'azure' in providers_lower:
        sections.append("""
## Azure/AKS Investigation:
- Check cluster status: cloud_tool('azure', 'aks show --name CLUSTER_NAME --resource-group RG_NAME')
- **IMPORTANT**: Get cluster credentials first: cloud_tool('azure', 'aks get-credentials --name CLUSTER_NAME --resource-group RG_NAME')
- Get pod details: cloud_tool('azure', 'kubectl get pods -n NAMESPACE -o wide')
- Describe problematic pods: cloud_tool('azure', 'kubectl describe pod POD_NAME -n NAMESPACE')
- Check pod logs: cloud_tool('azure', 'kubectl logs POD_NAME -n NAMESPACE --since=1h')
- Check pod metrics: cloud_tool('azure', 'kubectl top pod POD_NAME -n NAMESPACE')
- Check events: cloud_tool('azure', 'kubectl get events -n NAMESPACE --sort-by=.lastTimestamp')
- Check node health: cloud_tool('azure', 'kubectl describe node NODE_NAME')
- Query Azure Monitor: cloud_tool('azure', 'monitor log-analytics query -w WORKSPACE_ID --analytics-query "QUERY"')
- Check VMs: cloud_tool('azure', 'vm list --output table')
- Check resource groups: cloud_tool('azure', 'group list')
- Check NSGs: cloud_tool('azure', 'network nsg list')""")

    # OVH-specific instructions
    if 'ovh' in providers_lower:
        sections.append("""
## OVH Investigation:
- List projects: cloud_tool('ovh', 'cloud project list --json')
- List instances: cloud_tool('ovh', 'cloud instance list --cloud-project PROJECT_ID --json')
- Check instance details: cloud_tool('ovh', 'cloud instance get INSTANCE_ID --cloud-project PROJECT_ID --json')
- List Kubernetes clusters: cloud_tool('ovh', 'cloud kube list --cloud-project PROJECT_ID --json')
- Get kubeconfig: cloud_tool('ovh', 'cloud kube kubeconfig generate CLUSTER_ID --cloud-project PROJECT_ID')
- Then use kubectl: terminal_tool('kubectl --kubeconfig=/tmp/kubeconfig.yaml get pods -A')
- Check cluster nodes: terminal_tool('kubectl --kubeconfig=/tmp/kubeconfig.yaml get nodes')
- Check pod logs: terminal_tool('kubectl --kubeconfig=/tmp/kubeconfig.yaml logs POD_NAME -n NAMESPACE')
- **ON ANY OVH ERROR**: Use Context7 MCP to look up correct syntax:
  * For CLI errors: mcp_context7_get_library_docs(context7CompatibleLibraryID='/ovh/ovhcloud-cli', topic='COMMAND')
  * For Terraform errors: mcp_context7_get_library_docs(context7CompatibleLibraryID='/ovh/terraform-provider-ovh', topic='RESOURCE')""")

    # Scaleway-specific instructions
    if 'scaleway' in providers_lower:
        sections.append("""
## Scaleway Investigation:
- List instances: cloud_tool('scaleway', 'instance server list')
- Check instance details: cloud_tool('scaleway', 'instance server get SERVER_ID')
- List Kubernetes clusters: cloud_tool('scaleway', 'k8s cluster list')
- Get kubeconfig: cloud_tool('scaleway', 'k8s kubeconfig get CLUSTER_ID')
- Check cluster nodes: cloud_tool('scaleway', 'k8s node list cluster-id=CLUSTER_ID')
- List databases: cloud_tool('scaleway', 'rdb instance list')
- Check database logs: cloud_tool('scaleway', 'rdb log list instance-id=INSTANCE_ID')
- List load balancers: cloud_tool('scaleway', 'lb list')
- **ALWAYS use cloud_tool('scaleway', ...) NOT terminal_tool for Scaleway commands**""")

    # General kubectl/SSH instructions
    k8s_providers = {'gcp', 'aws', 'azure', 'ovh', 'scaleway'}
    if k8s_providers.intersection(set(providers_lower)):
        sections.append("""
## General Kubernetes Investigation (for any provider):
- Check all pods across namespaces: kubectl get pods -A
- Check resource usage: kubectl top pods -n NAMESPACE
- Check persistent volumes: kubectl get pv,pvc -A
- Check config maps: kubectl get configmaps -n NAMESPACE
- Check secrets (names only): kubectl get secrets -n NAMESPACE
- Check ingress: kubectl get ingress -A
- Check network policies: kubectl get networkpolicies -A""")

    # SSH investigation for VMs
    sections.append("""
## SSH Investigation (for VMs):
If you need to SSH into a VM for deeper investigation:
1. Generate SSH key if needed: terminal_tool('test -f ~/.ssh/aurora_key || ssh-keygen -t rsa -b 4096 -f ~/.ssh/aurora_key -N ""')
2. Get public key: terminal_tool('cat ~/.ssh/aurora_key.pub')
3. Add key to VM (provider-specific)
4. SSH with command: terminal_tool('ssh -i ~/.ssh/aurora_key -o StrictHostKeyChecking=no USER@IP "COMMAND"')
   - GCP: USER=admin
   - AWS: USER=ec2-user (Amazon Linux) or ubuntu (Ubuntu)
   - Azure: USER=azureuser
   - OVH: USER=debian or ubuntu
   - Scaleway: USER=root""")

    # Add note about tool names
    sections.append("""
## IMPORTANT - Tool Name Mapping:
In the examples above:
- cloud_tool() refers to the cloud_exec tool
- terminal_tool() refers to the terminal_exec tool
Use the actual tool names (cloud_exec, terminal_exec) when making calls.""")

    return "\n".join(sections)


def _get_github_context(user_id: str) -> Optional[Dict[str, str]]:
    """Check if user has GitHub connected and a repo selected."""
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        
        # Check if GitHub is connected
        github_creds = get_credentials_from_db(user_id, "github")
        if not github_creds or not github_creds.get("access_token"):
            return None
            
        # Check if a repo is selected
        selection = get_credentials_from_db(user_id, "github_repo_selection")
        if not selection or not selection.get("branch"):
            return None
            
        repo = selection.get("repository")
        branch = selection.get("branch")
        
        return {
            "repo_full_name": repo.get("full_name"),
            "branch_name": branch.get("name"),
            "repo_url": repo.get("html_url")
        }
    except Exception as e:
        logger.warning(f"Error checking GitHub context: {e}")
        return None


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
) -> str:
    """Build a comprehensive, provider-aware RCA prompt."""
    # Fetch providers if not provided
    if not providers and user_id:
        providers = get_user_providers(user_id)

    providers = providers or []
    providers_lower = [p.lower() for p in providers]

    # Format alert details
    title = alert_details.get('title', 'Unknown Alert')
    status = alert_details.get('status', 'unknown')
    labels = alert_details.get('labels', {})
    message = alert_details.get('message', '')
    values = alert_details.get('values', '')

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

    # Connected providers section
    prompt_parts.extend([
        "",
        "## CONNECTED PROVIDERS:",
        f"You have access to: {', '.join(providers) if providers else 'No providers detected - check user configuration'}",
    ])

    # GitHub Integration
    if user_id:
        github_context = _get_github_context(user_id)
        if github_context:
            repo_full_name = github_context['repo_full_name']
            branch_name = github_context['branch_name']
            
            # Extract owner and repo name safely
            try:
                owner, repo_name = repo_full_name.split('/')
            except ValueError:
                owner, repo_name = repo_full_name, ""
            
            prompt_parts.extend([
                "",
                "## GITHUB REPOSITORY CONTEXT:",
                f"- **Connected Repository**: {repo_full_name}",
                f"- **Branch**: {branch_name}",
                "",
                "### GITHUB RCA TOOL (RECOMMENDED):",
                "Use the unified `github_rca` tool for efficient GitHub investigation.",
                "This tool provides timeline correlation and structured output.",
                "",
                "**IMPORTANT: Repository Resolution**",
                "- Omit `repo=` to auto-resolve from Knowledge Base documents (runbooks)",
                "- Pass `repo='owner/repo'` explicitly to investigate a DIFFERENT repository than KB suggests",
                "- Resolution order: explicit param → KB Memory → KB Documents → connected repo",
                "",
                "**Quick Investigation Commands:**",
                "1. **Check deployments**: `github_rca(action='deployment_check')`",
                "2. **List recent commits**: `github_rca(action='commits')`",
                "3. **Check merged PRs**: `github_rca(action='pull_requests')`",
                "4. **Get commit diff**: `github_rca(action='diff', commit_sha='COMMIT_SHA')`",
                "",
                "**Timeline Correlation:**",
                "- By default, the tool uses current time and searches 24 hours back",
                "- Pass `incident_time` only if you have a specific timestamp from the alert",
                "- Use `time_window_hours` to adjust search scope if needed",
                "",
                "**Recommended Investigation Flow:**",
                "1. First call `github_rca(action='deployment_check')` to see recent workflow runs",
                "2. Then call `github_rca(action='commits')` to list commits in time window",
                "3. For suspicious commits, use `github_rca(action='diff', commit_sha='...')` to see changes",
                "4. Check `github_rca(action='pull_requests')` for recently merged PRs",
                "",
                "### IMPORTANT NOTES:",
                "- The repository is REMOTE. Do NOT use `ls`, `cat`, `cd`, or terminal commands for GitHub.",
                "- Do NOT attempt to `git clone` the repository.",
                "- ALWAYS check GitHub BEFORE diving into infrastructure commands.",
                "- Look for changes in: config files, k8s manifests, Terraform, dependencies.",
                "",
                "FAILURE TO CHECK CODE IS A FAILURE OF THE INVESTIGATION.",
                "",
                "### FIX SUGGESTIONS (When applicable):",
                "When you identify a code issue that caused the incident, use `github_fix` to suggest a fix:",
                "```",
                "github_fix(",
                "    file_path='config/deployment.yaml',",
                "    suggested_content='[complete fixed file content]',",
                "    fix_description='What this fix does',",
                "    root_cause_summary='Why this caused the issue'",
                ")",
                "```",
                "",
                "**Guidelines for suggesting fixes:**",
                "- Only suggest fixes when you have HIGH CONFIDENCE in the root cause",
                "- Provide the COMPLETE file content, not just the diff",
                "- The fix is stored for user review - never applied automatically",
                "- Best for: config changes, YAML values, environment variables, resource limits",
                "- The user can edit your suggestion before creating a PR",
            ])

    # Provider-specific investigation section
    provider_section = _build_provider_investigation_section(providers, user_id)
    if provider_section:
        prompt_parts.extend([
            "",
            "## PROVIDER-SPECIFIC INVESTIGATION STEPS:",
            provider_section,
        ])

    # Jenkins CI/CD context: inject recent deployments + investigation instructions
    if user_id and _has_jenkins_connected(user_id):
        alert_service = alert_details.get('labels', {}).get('service', '') or ''
        if source == 'netdata':
            alert_service = alert_details.get('host', '') or ''

        recent_deploys = _get_recent_jenkins_deployments(user_id, alert_service, provider="jenkins")
        prompt_parts.extend([
            "",
            "## JENKINS CI/CD INTEGRATION:",
            "Jenkins is connected. Use the `jenkins_rca` tool to investigate CI/CD activity.",
            "",
        ])

        if recent_deploys:
            prompt_parts.append("### RECENT DEPLOYMENTS (potential change correlation):")
            for dep in recent_deploys:
                ts = dep.get("webhook_received_at", "?")
                commit_sha = dep.get('commit_sha') or '?'
                prompt_parts.append(
                    f"- [{dep['result']}] {dep['service']} → {dep.get('environment', '?')} "
                    f"received {ts} (commit: {commit_sha[:8]}, "
                    f"build: #{dep.get('build_number', '?')})"
                )
                if dep.get("trace_id"):
                    prompt_parts.append(f"  OTel Trace ID: {dep['trace_id']}")
            prompt_parts.append("")

        prompt_parts.extend([
            "### Jenkins Investigation Commands:",
            "- Check recent deployments: `jenkins_rca(action='recent_deployments', service='SERVICE')`",
            "- Get build details with commits: `jenkins_rca(action='build_detail', job_path='JOB', build_number=N)`",
            "- Get pipeline stage breakdown: `jenkins_rca(action='pipeline_stages', job_path='JOB', build_number=N)`",
            "- Get stage-specific logs: `jenkins_rca(action='stage_log', job_path='JOB', build_number=N, node_id='NODE')`",
            "- Get build console output: `jenkins_rca(action='build_logs', job_path='JOB', build_number=N)`",
            "- Get test failures: `jenkins_rca(action='test_results', job_path='JOB', build_number=N)`",
            "- Blue Ocean run data: `jenkins_rca(action='blue_ocean_run', pipeline_name='PIPELINE', run_number=N)`",
            "- Check OTel trace context: `jenkins_rca(action='trace_context', deployment_event_id=ID)`",
            "",
            "**IMPORTANT**: Recent deployments are a leading indicator of root cause.",
            "Always check if a deployment occurred shortly before the alert fired.",
        ])

    # CloudBees CI/CD context (same API as Jenkins, separate credentials)
    if user_id and _has_cloudbees_connected(user_id):
        alert_service = alert_details.get('labels', {}).get('service', '') or ''
        if source == 'netdata':
            alert_service = alert_details.get('host', '') or ''

        recent_deploys = _get_recent_jenkins_deployments(user_id, alert_service, provider="cloudbees")
        prompt_parts.extend([
            "",
            "## CLOUDBEES CI/CD INTEGRATION:",
            "CloudBees CI is connected. Use the `cloudbees_rca` tool to investigate CI/CD activity.",
            "",
        ])

        if recent_deploys:
            prompt_parts.append("### RECENT DEPLOYMENTS (potential change correlation):")
            for dep in recent_deploys:
                ts = dep.get("webhook_received_at", "?")
                commit_sha = dep.get('commit_sha') or '?'
                prompt_parts.append(
                    f"- [{dep['result']}] {dep['service']} → {dep.get('environment', '?')} "
                    f"received {ts} (commit: {commit_sha[:8]}, "
                    f"build: #{dep.get('build_number', '?')})"
                )
                if dep.get("trace_id"):
                    prompt_parts.append(f"  OTel Trace ID: {dep['trace_id']}")
            prompt_parts.append("")

        prompt_parts.extend([
            "### CloudBees Investigation Commands:",
            "- Check recent deployments: `cloudbees_rca(action='recent_deployments', service='SERVICE')`",
            "- Get build details with commits: `cloudbees_rca(action='build_detail', job_path='JOB', build_number=N)`",
            "- Get pipeline stage breakdown: `cloudbees_rca(action='pipeline_stages', job_path='JOB', build_number=N)`",
            "- Get stage-specific logs: `cloudbees_rca(action='stage_log', job_path='JOB', build_number=N, node_id='NODE')`",
            "- Get build console output: `cloudbees_rca(action='build_logs', job_path='JOB', build_number=N)`",
            "- Get test failures: `cloudbees_rca(action='test_results', job_path='JOB', build_number=N)`",
            "- Blue Ocean run data: `cloudbees_rca(action='blue_ocean_run', pipeline_name='PIPELINE', run_number=N)`",
            "- Check OTel trace context: `cloudbees_rca(action='trace_context', deployment_event_id=ID)`",
            "",
            "**IMPORTANT**: Recent deployments are a leading indicator of root cause.",
            "Always check if a deployment occurred shortly before the alert fired.",
        ])

    # Aurora Learn: Inject context from similar past incidents
    if user_id:
        alert_service = alert_details.get('labels', {}).get('service', '') or ''
        if source == 'netdata':
            alert_service = alert_details.get('host', '') or ''
        similar_context = _get_similar_good_rcas_context(
            user_id=user_id,
            alert_title=title,
            alert_service=alert_service,
            source_type=source,
        )
        if similar_context:
            prompt_parts.append(similar_context)

    # Critical persistence instructions
    prompt_parts.extend([
        "",
        "## CRITICAL INVESTIGATION REQUIREMENTS:",
        "",
    ])
    
    # Add aggressive persistence prompts only if cost optimization is disabled
    # The immediate action required due to the AgentExecutor which assumes agent is done when it sends a text chunk without a tool call.
    if os.getenv("RCA_OPTIMIZE_COSTS", "").lower() != "true":
        prompt_parts.extend([
            "### PERSISTENCE IS MANDATORY:",
            "- **MINIMUM**: Make AT LEAST 15-20 tool calls before concluding",
            "- **DO NOT STOP** after 2-3 commands - keep investigating until you find the EXACT root cause",
            "- **SPEND TIME**: Investigation should take AT LEAST 3-5 minutes of active tool usage",
            "- **IF BLOCKED**: Try 3-5 alternative approaches before giving up on any single avenue",
            "- **COMMAND FAILURES ARE NOT STOPPING POINTS**: When a command fails, try alternatives immediately",
            "",
            "### IMMEDIATE ACTION REQUIRED:",
            "- **DO NOT** output a plan or text explanation first.",
            "- **DO NOT** say 'I will start by...'",
            "- **IMMEDIATELY** call the first tool (e.g., `list_gcp_resources` or `kubectl`).",
            "- UNLESS YOU ARE DONE, your response MUST contain a tool call.",
            "- NOT PROVIDING A TOOL CALL WILL END THE INVESTIGATION AUTOMATICALLY",
            "",
        ])
    
    prompt_parts.extend([
        "### INVESTIGATION DEPTH:",
        "1. Start broad - understand the overall system state",
        "2. Identify the affected component(s)",
        "3. Drill down into specifics - logs, metrics, configurations",
        "4. Check related/dependent resources",
        "5. Look for recent changes that correlate with the issue",
        "6. Compare with healthy resources of the same type",
        "7. Check resource quotas, limits, and constraints",
        "8. Examine network connectivity and security rules",
        "9. Verify IAM permissions and service accounts",
        "10. Review historical patterns if available",
        "",
        "### ERROR RESILIENCE:",
        "- If cloud monitoring/metrics commands fail -> use kubectl directly",
        "- If kubectl fails -> check cloud provider CLI alternatives",
        "- If one log source fails -> try another (kubectl logs, cloud logging, container logs)",
        "- If a resource isn't found -> check other namespaces, regions, or projects",
        "- **ALWAYS have 3-4 backup approaches ready**",
        "",
        "### WHAT TO INVESTIGATE:",
        "- Resource STATUS and HEALTH (running, pending, failed, etc.)",
        "- LOGS for error messages, warnings, stack traces",
        "- METRICS for CPU, memory, disk, network anomalies",
        "- CONFIGURATIONS for misconfigurations or invalid values",
        "- EVENTS for recent state changes",
        "- DEPENDENCIES for cascading failures",
        "- RECENT CHANGES or deployments that correlate with the issue",
        "",
        "## OUTPUT REQUIREMENTS:",
        "",
        "### Your analysis MUST include:",
        "1. **Summary**: Brief description of the incident",
        "2. **Investigation Steps**: Document EVERY tool call and what it revealed",
        "3. **Evidence**: Show specific log entries, metric values, config snippets",
        "4. **Root Cause**: Clearly state the EXACT root cause with supporting evidence",
        "5. **Impact**: Describe what was affected and how",
        "6. **Remediation**: Specific, actionable steps to fix the issue",
        "",
        "### Remember:",
        "- You are in READ-ONLY mode - investigate thoroughly but do NOT make any changes",
        "- The user expects you to find the EXACT root cause, not surface-level symptoms",
        "- Keep digging until you have definitive answers",
        "- Never conclude with 'unable to determine' without exhausting all investigation avenues",
        "",
        "## BEGIN INVESTIGATION NOW",
        "Start by understanding the scope of the issue, then systematically investigate using the tools and approaches above.",
    ])

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
