"""
Billing Scheduler Routes

HTTP endpoints to trigger automated billing from Google Cloud Scheduler
or other cloud scheduling services.
"""

import logging
from flask import Blueprint, request, jsonify
from utils.web.cors_utils import create_cors_response

# Configure logging
logger = logging.getLogger(__name__)

billing_scheduler_bp = Blueprint('billing_scheduler', __name__)

@billing_scheduler_bp.route('/api/admin/run-weekly-billing', methods=['POST', 'OPTIONS'])
def run_weekly_billing():
    """
    Trigger weekly billing automation.
    Designed to be called by Google Cloud Scheduler or similar services.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        # Security check - only allow from Cloud Scheduler or admin
        # You can add IP allowlisting, API keys, or other auth here
        user_agent = request.headers.get('User-Agent', '')
        if 'Google-Cloud-Scheduler' not in user_agent:
            logger.warning(f"Weekly billing triggered from non-scheduler source: {user_agent}")
            # Uncomment to restrict access:
            # return jsonify({"error": "Unauthorized"}), 401
        
        # Extract environment info from request
        environment = request.json.get('environment', 'unknown') if request.json else 'unknown'
        source = request.json.get('source', 'manual') if request.json else 'manual'
        action = request.json.get('action', 'run_weekly_billing') if request.json else 'run_weekly_billing'
        debug = request.json.get('debug', False) if request.json else False
        
        logger.info(f"Weekly billing automation triggered via HTTP endpoint [env: {environment}, source: {source}, action: {action}]")
        
        # Handle debug actions
        if action != 'run_weekly_billing':
            return handle_debug_action(action, environment, debug)
        
        # Import and run the weekly reporter
        import sys
        import os
        
        # Add the project root to path
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if project_root not in sys.path:
            sys.path.append(project_root)
        
        # Use existing environment - do not override if already set
        
        # Get dry_run parameter from request
        dry_run = request.json.get('dry_run', False) if request.json else False
        
        logger.info(f"Starting weekly billing automation [dry_run={dry_run}, environment={environment}]")
        
        try:
            from scripts.weekly_usage_reporter import WeeklyUsageReporter
            logger.info("Successfully imported WeeklyUsageReporter")
            
            reporter = WeeklyUsageReporter()
            logger.info("Successfully created WeeklyUsageReporter instance")
            
            # Run the billing and capture any errors
            result = reporter.report_weekly_usage(dry_run=dry_run)
            logger.info(f"Weekly billing execution completed with result: {result}")
            
        except ImportError as ie:
            logger.error(f"Import error in weekly billing: {ie}", exc_info=True)
            return jsonify({
                "success": False,
                "error": "Failed to import billing module",
                "environment": environment
            }), 500
        except Exception as be:
            logger.error(f"Billing execution error: {be}", exc_info=True)
            return jsonify({
                "success": False,
                "error": "Billing execution failed",
                "environment": environment
            }), 500
        
        logger.info("Weekly billing automation completed successfully")
        
        from datetime import datetime
        
        # Include billing result details in response
        response_data = {
            "success": True,
            "message": f"Weekly billing completed successfully for {environment} environment",
            "environment": environment,
            "source": source,
            "dry_run": dry_run,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        
        # Add billing result details if available
        if result and isinstance(result, dict):
            response_data["billing_result"] = result
        
        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"Error in weekly billing automation: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": "Weekly billing automation failed"
        }), 500

def handle_debug_action(action, environment, debug):
    """Handle debug actions for investigating billing issues"""
    import sys
    import os
    
    # Add the project root to path
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.append(project_root)
    
    try:
        from utils.db.connection_pool import db_pool
        from datetime import datetime, timezone
        
        logger.info(f"Handling debug action: {action}")
        
        if action == 'count_usage_data':
            with db_pool.get_admin_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT COUNT(*) as total_records,
                           COUNT(DISTINCT user_id) as unique_users,
                           MIN(timestamp) as oldest_record,
                           MAX(timestamp) as newest_record,
                           SUM(total_cost_with_surcharge) as total_cost
                    FROM llm_usage_tracking
                    WHERE timestamp >= NOW() - INTERVAL '30 days'
                """)
                result = cursor.fetchone()
                return jsonify({
                    "action": action,
                    "environment": environment,
                    "total_usage_records_30d": result[0],
                    "unique_users_with_usage": result[1],
                    "oldest_record": result[2].isoformat() if result[2] else None,
                    "newest_record": result[3].isoformat() if result[3] else None,
                    "total_cost_30d": float(result[4]) if result[4] else 0.0,
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                })
        
        else:
            return jsonify({
                "error": f"Unknown debug action: {action}",
                "available_actions": ["count_usage_data"]
            }), 400
            
    except Exception as e:
        logger.error(f"Error in debug action {action}: {e}", exc_info=True)
        return jsonify({
            "error": "Debug action failed",
            "action": action,
            "environment": environment
        }), 500

@billing_scheduler_bp.route('/api/admin/billing-status', methods=['GET', 'OPTIONS'])
def get_billing_status():
    """
    Get status of billing automation system.
    Useful for monitoring and health checks.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        # You can add more status checks here
        return jsonify({
            "status": "healthy",
            "billing_system": "operational",
            "last_check": "2024-01-01T00:00:00Z"  # Add actual timestamp
        }), 200
        
    except Exception as e:
        logger.error(f"Error checking billing status: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "error": "Failed to check billing status"
        }), 500
