import logging
from celery import shared_task
from psycopg2.extras import Json
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

def _generate_ai_triage(finding: dict) -> dict:
    """
    Placeholder/mock for Agentic AI triage.
    In a full LangGraph implementation, this sends the finding
    to the LLM with strict instructions to generate only:
    - Summary
    - Risk Level
    - Suggested Fix
    (NO AUTO-REMEDIATION).
    """
    title = finding.get("Title", "Unknown Finding")
    desc = finding.get("Description", "")
    
    # In reality, you would call `litellm` or your LangGraph agent here.
    return {
        "summary": f"Security finding detected: {title}. Desc: {desc}",
        "risk_level": finding.get("Severity", {}).get("Label", "UNKNOWN"),
        "suggested_fix": "Please review the affected resources and apply least privilege principles based on the AWS finding."
    }

@shared_task
def process_securityhub_finding(payload: dict, org_id: str):
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
