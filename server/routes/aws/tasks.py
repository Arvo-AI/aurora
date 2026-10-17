"""
Background tasks for processing AWS Security Hub findings.
"""
import logging
from celery import shared_task
from psycopg2.extras import Json
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

def _generate_ai_triage(finding: dict) -> dict:
    """
    Generate triage summary and suggested fixes based on finding details.
    Extracts affected resources to build an actionable checklist.
    """
    title = finding.get("Title", "Unknown Finding")
    desc = finding.get("Description", "")
    severity_info = finding.get("Severity")
    severity = severity_info.get("Label", "UNKNOWN") if isinstance(severity_info, dict) else "UNKNOWN"
    
    resources = finding.get("Resources")
    if not isinstance(resources, list):
        resources = []
    
    resource_names = []
    service_types = []
    for res in resources:
        if not isinstance(res, dict):
            continue
        if res.get("Id"):
            resource_names.append(res["Id"])
        if res.get("Type"):
            service_types.append(res["Type"])
            
    resource_names_str = ", ".join(resource_names) if resource_names else "Unknown resources"
    service_types_str = ", ".join(set(service_types)) if service_types else "Unknown services"
    
    urgency_prefix = "URGENT (Critical/High Severity)" if severity in ["CRITICAL", "HIGH"] else "STANDARD"
    
    suggested_fix = f"""{urgency_prefix}: Review affected resources.

Affected Services: {service_types_str}
Affected Resources: {resource_names_str}

Action Checklist:
[ ] 1. Identify affected resources ({resource_names_str})
[ ] 2. Revoke/adjust IAM permissions
[ ] 3. Enable logging/monitoring
[ ] 4. Apply recommended configuration changes
[ ] 5. Verify"""

    return {
        "summary": f"Security finding detected: {title}. Desc: {desc}",
        "risk_level": severity,
        "suggested_fix": suggested_fix
    }

@shared_task
def process_securityhub_finding(payload: dict, org_id: str):
    """
    Background task to process Security Hub finding webhook payloads.
    Generates AI triage context and upserts records to PostgreSQL.
    """
    logger.info("[SECURITY_HUB] Processing background task for event %s", sanitize(payload.get('id')))

    detail = payload.get("detail", {})
    findings = detail.get("findings", [])

    if not findings:
        logger.warning("[SECURITY_HUB] No findings found in payload detail.")
        return

    from utils.auth.stateless_auth import set_rls_context

    saved_count = 0
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM users WHERE org_id = %s LIMIT 1", (org_id,))
                user_row = cursor.fetchone()
                if not user_row:
                    logger.error("[SECURITY_HUB] No user found for org %s to set RLS context", sanitize(org_id))
                    return
                user_id = user_row[0]
                
                set_rls_context(cursor, conn, user_id, log_prefix="[SECURITY_HUB]")
                for finding in findings:
                    if not isinstance(finding, dict):
                        continue
                    
                    finding_id = finding.get("Id")
                    if not finding_id:
                        continue
                    
                    title = finding.get("Title", "Untitled Finding")
                    severity_info = finding.get("Severity")
                    severity_label = severity_info.get("Label", "UNKNOWN") if isinstance(severity_info, dict) else "UNKNOWN"
                    source = finding.get("ProductName", "AWS Security Hub")
                    
                    # Human-in-the-loop: Agent ONLY suggests
                    ai_triage = _generate_ai_triage(finding)

                    query = """
                        INSERT INTO aws_security_findings (
                            org_id, finding_id, source, title, severity_label, 
                            payload, ai_summary, ai_risk_level, ai_suggested_fix
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (org_id, finding_id) DO UPDATE SET
                            title = EXCLUDED.title,
                            severity_label = EXCLUDED.severity_label,
                            payload = EXCLUDED.payload,
                            source = EXCLUDED.source,
                            ai_summary = EXCLUDED.ai_summary,
                            ai_risk_level = EXCLUDED.ai_risk_level,
                            ai_suggested_fix = EXCLUDED.ai_suggested_fix,
                            updated_at = NOW()
                    """
                    
                    cursor.execute(query, (
                        org_id,
                        finding_id,
                        source,
                        title,
                        severity_label,
                        Json(finding),
                        ai_triage["summary"],
                        ai_triage["risk_level"],
                        ai_triage["suggested_fix"]
                    ))
                    saved_count += 1
            
            conn.commit()
            logger.info("[SECURITY_HUB] Successfully processed & UPSERTED %d findings for org %s", saved_count, sanitize(org_id))

    except Exception:
        logger.exception("[SECURITY_HUB] Failed to process findings into DB")
        raise
