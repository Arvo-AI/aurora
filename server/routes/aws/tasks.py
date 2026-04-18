import logging
from celery import shared_task
from psycopg2.extras import Json
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

def _generate_ai_triage(finding: dict) -> dict:
    """
    Generate triage summary and suggested fixes based on finding details.
    Extracts affected resources to build an actionable checklist.
    """
    title = finding.get("Title", "Unknown Finding")
    desc = finding.get("Description", "")
    severity = finding.get("Severity", {}).get("Label", "UNKNOWN")
    
    resources = finding.get("Resources", [])
    
    resource_names = []
    service_types = []
    for res in resources:
        if res.get("Id"): resource_names.append(res["Id"])
        if res.get("Type"): service_types.append(res["Type"])
            
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
    logger.info(f"[SECURITY_HUB] Processing background task for event {payload.get('id')}")

    detail = payload.get("detail", {})
    findings = detail.get("findings", [])

    if not findings:
        logger.warning("[SECURITY_HUB] No findings found in payload detail.")
        return

    saved_count = 0
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                for finding in findings:
                    if not isinstance(finding, dict):
                        continue
                    
                    finding_id = finding.get("Id")
                    if not finding_id:
                        continue
                    
                    title = finding.get("Title", "Untitled Finding")
                    severity_label = finding.get("Severity", {}).get("Label", "UNKNOWN")
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
            logger.info(f"[SECURITY_HUB] Successfully processed & UPSERTED {saved_count} findings for org {org_id}")

    except Exception as exc:
        logger.exception("[SECURITY_HUB] Failed to process findings into DB")
        raise
