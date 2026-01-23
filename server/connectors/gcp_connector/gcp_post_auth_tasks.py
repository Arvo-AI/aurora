import logging
import json
import base64
import urllib.parse
from celery_config import celery_app
from .auth_compatibility import (
    get_credentials,
    enable_all_required_apis_for_all_projects, ensure_aurora_full_access,
    allow_public_access_for_all_projects, get_project_list
)
from .billing import has_active_billing
from .gcp.projects import select_best_project
from .gcp.apis import enable_single_api
from utils.auth.stateless_auth import get_credentials_from_db, get_user_preference, store_user_preference
from connectors.gcp_connector.auth.service_accounts import generate_sa_access_token
import time

@celery_app.task(bind=True)
def gcp_post_auth_setup_task(self, user_id, selected_project_ids=None):
    """
    Handle time-consuming GCP setup operations after OAuth callback.
    This contains EXACTLY the same code from the callback function's slow operations.
    """
    try:
        logging.info(f"Starting GCP post-auth setup for user {user_id}")
        
        # Check project count before proceeding (only if not already filtered)
        if not selected_project_ids:
            token_data = get_credentials_from_db(user_id, 'gcp')
            if token_data:
                creds = get_credentials(token_data)
                all_projects = get_project_list(creds)
                
                # Count eligible projects (only check billing - SA permissions don't exist yet on first auth)
                eligible = []
                for p in all_projects:
                    pid = p.get('projectId')
                    if not pid:
                        continue
                    if has_active_billing(pid, creds):
                        eligible.append({'projectId': pid, 'name': p.get('name', pid)})
                
                # If > 5, return early with project list for user selection
                if len(eligible) > 5:
                    logging.info(f"Too many projects ({len(eligible)}), needs selection")
                    return {
                        'status': 'needs_selection',
                        'eligible_projects': eligible,
                        'count': len(eligible)
                    }
        
        # Update progress: Starting
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Initializing GCP setup...', 'progress': 5, 'step': 1, 'total_steps': 7}
        )
        
        # Get stored token data from database
        token_data = get_credentials_from_db(user_id, 'gcp')
        if not token_data:
            raise ValueError(f"No GCP credentials found for user {user_id}")
        
        # EXACT SAME CODE FROM CALLBACK STARTING AT LINE 212:
        
        credentials = get_credentials(token_data)
        
        # Update progress: Validating projects
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Validating GCP projects...', 'progress': 15, 'step': 2, 'total_steps': 7}
        )
        
        projects = get_project_list(credentials)
        if not projects:
            logging.warning("No GCP projects found during login callback for user %s", user_id)
            return {
                'status': 'FAILED',
                'redirect_params': 'login=gcp_no_projects'
            }
        
        # Filter projects early if selected_project_ids provided
        if selected_project_ids:
            projects_to_check = [p for p in projects if p.get('projectId') in selected_project_ids]
            logging.info(f"Checking billing for {len(projects_to_check)} pre-selected projects")
        else:
            projects_to_check = projects
        
        # Check if at least one project has billing
        for proj in projects_to_check:
            project_id = proj.get("projectId")
            if has_active_billing(project_id, credentials):
                break
        else:
            logging.warning("No GCP project with active billing found for user %s", user_id)
            return {
                'status': 'FAILED', 
                'redirect_params': 'login=gcp_failed_billing'
            }

        # Determine which projects to setup based on selection
        if selected_project_ids:
            # Filter projects to only the selected ones
            projects_to_setup = [p for p in projects if p.get('projectId') in selected_project_ids]
            logging.info(f"Running setup for {len(projects_to_setup)} selected projects")
        else:
            # Use all projects
            projects_to_setup = projects
            logging.info(f"Running setup for all {len(projects_to_setup)} projects")

        # Update progress: Enabling APIs
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Enabling required APIs...', 'progress': 30, 'step': 3, 'total_steps': 7}
        )
        
        # Enable all required APIs for projects (25 APIs total)
        # Only process the selected/filtered projects, not all accessible projects
        try:
            logging.info(f"Enabling all required APIs for {len(projects_to_setup)} projects (25 APIs)")
            results = enable_all_required_apis_for_all_projects(credentials, projects=projects_to_setup)
            if results:
                logging.info(f"API enablement results: {results}")
        except Exception as e:
            logging.error(f"Error enabling required APIs: {e}")
            # Continue with the flow even if API enablement fails

        # Determine root project preference (used for service account setup)
        root_project_id = get_user_preference(user_id, 'gcp_root_project')
        if not root_project_id:
            root_project_id = select_best_project(credentials, projects, user_id)
            if root_project_id:
                logging.info(f"Detected and saving root project preference: {root_project_id}")
                store_user_preference(user_id, 'gcp_root_project', root_project_id)

        # Update progress: Creating service accounts
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Creating Aurora service accounts...', 'progress': 50, 'step': 4, 'total_steps': 7}
        )
        
        # Perform Aurora full-access setup (service account creation & IAM grants)
        # Note: It's OK if some projects fail - we'll continue with the ones that work
        setup_success = False
        try:
            logging.info("Setting up Aurora full-access permissions via SDK")
            # Pass the filtered project list to only setup selected projects
            ensure_aurora_full_access(credentials, user_id, projects_to_setup, root_project_id_override=root_project_id)
            logging.info("Aurora full-access setup completed successfully")
            setup_success = True
        except ValueError as permission_error:
            # Permission-related errors - log but continue (some projects may work)
            logging.warning(f"Permission validation failed (will continue with accessible projects): {permission_error}")
        except Exception as e:
            logging.warning(f"Aurora full-access setup had errors (will continue with accessible projects): {e}")

        # Update progress: Updating policies
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Configuring organization policies...', 'progress': 70, 'step': 5, 'total_steps': 7}
        )
        
        # Update Org Policy to allow public access for Cloud Run (allUsers principal)
        # Only process the selected/filtered projects, not all accessible projects
        try:
            logging.info(f"Updating Org Policy to allow public Cloud Run access for {len(projects_to_setup)} projects")
            policy_results = allow_public_access_for_all_projects(credentials, projects=projects_to_setup)
            if policy_results:
                logging.info(f"Org Policy update results: {policy_results}")
        except Exception as e:
            logging.error(f"Error updating Org Policy for public Cloud Run access: {e}")
            policy_results = None  # Ensure variable is defined on error

        # Determine if Org Policy changes were successful for all projects
        policy_failed = True  # Assume failure by default
        if policy_results is not None:
            policy_failed = not all(policy_results.values())

        policy_param = "policy_failed" if policy_failed else "policy_ok"

        # Encode policy_results as base64 JSON for transfer (compact) if available
        policy_details_param = ""
        try:
            if policy_results is not None and len(policy_results) > 0:
                json_str = json.dumps(policy_results, separators=(",", ":"))
                b64_bytes = base64.urlsafe_b64encode(json_str.encode())
                policy_details_param = urllib.parse.quote(b64_bytes.decode())
        except Exception as _enc_err:
            logging.warning(f"Failed to encode policy_results: {_enc_err}")

        # Propagation verification (DEBUG LOGS)
        logging.info("=== PROPAGATION VERIFICATION START ===")
        logging.info(f"[DEBUG] Verifying propagation for {len(projects)} projects")
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Verifying permissions across projects...', 'progress': 75, 'step': 6, 'total_steps': 7}
        )
        verified = {p['projectId']: False for p in projects}
        verified_count = 0
        attempts = 0
        max_attempts = 20
        while verified_count < len(projects) and attempts < max_attempts:
            pending = [pid for pid, v in verified.items() if not v][:10]
            logging.info(f"[DEBUG] Propagation attempt {attempts + 1}/{max_attempts}: checking {len(pending)} projects ({verified_count}/{len(projects)} already verified)")
            if not pending:
                logging.info("[DEBUG] Propagation verification complete early: all projects verified")
                break
            batch_success = 0
            for pid in pending:
                try:
                    logging.info(f"[DEBUG]  Checking token gen for project {pid}")
                    generate_sa_access_token(user_id, selected_project_id=pid)
                    verified[pid] = True
                    batch_success += 1
                    logging.info(f"[DEBUG]  ✓ {pid} verified successfully")
                except Exception as e:
                    logging.warning(f"[DEBUG] Propagation check failed for {pid}: {str(e)}")
            verified_count += batch_success
            attempts += 1
            self.update_state(state='PROGRESS', meta={
                'status': f'Verifying permissions: {verified_count}/{len(projects)} ({attempts}/{max_attempts})',
                'progress': 75 + min((verified_count / len(projects)) * 15, 15),
                'propagation': {'current': verified_count, 'total': len(projects)},
                'step': 6, 'total_steps': 7
            })
            logging.info(f"[DEBUG] Batch {attempts} complete: +{batch_success} verified (total {verified_count}/{len(projects)})")
            time.sleep(30)  # Give IAM bindings time to propagate; only holds one Celery slot, not the whole worker
        is_fully_verified = verified_count == len(projects)
        propagation_status = 'FULL' if is_fully_verified else 'PARTIAL'
        logging.info(f"=== PROPAGATION VERIFICATION FINISH: {propagation_status} ({verified_count}/{len(projects)}) ===")

        redirect_params = f"login=gcp_success&policy={policy_param}"
        if policy_details_param:
            redirect_params += f"&policy_details={policy_details_param}"

        # Add warning if setup had partial failures
        if not setup_success:
            redirect_params += "&partial_setup=true"
        if not is_fully_verified:
            redirect_params += "&propagation_partial=true"

        # Update progress: Finalizing
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Finalizing setup...', 'progress': 90, 'step': 7, 'total_steps': 7}
        )
        
        logging.info(f"GCP post-auth setup completed for user {user_id} (setup_success={setup_success})")

        return {
            'status': 'SUCCESS',
            'redirect_params': redirect_params,
            'propagation_status': propagation_status,
            'verified_projects': verified_count,
            'total_projects': len(projects)
        }

    except Exception as e:
        logging.error(f"Error during GCP post-auth setup for user {user_id}: {e}")

        return {
            'status': 'FAILED',
            'redirect_params': 'login=gcp_failed',
            'propagation_status': 'FAILED',
            'verified_projects': 0,
            'total_projects': 0
        } 
