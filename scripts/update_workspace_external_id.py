#!/usr/bin/env python3
"""
Script to update AWS external ID for a workspace.
Usage: python3 scripts/update_workspace_external_id.py <workspace_id> [new_external_id]
"""
import sys
import os

# Add server directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))

from utils.workspace.workspace_utils import update_workspace_external_id, get_workspace_by_id
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/update_workspace_external_id.py <workspace_id> [new_external_id]")
        sys.exit(1)
    
    workspace_id = sys.argv[1]
    new_external_id = sys.argv[2] if len(sys.argv) > 2 else None
    
    # Verify workspace exists first
    workspace = get_workspace_by_id(workspace_id)
    if not workspace:
        print(f"Error: Workspace {workspace_id} not found")
        sys.exit(1)
    
    print(f"Workspace found:")
    print(f"  ID: {workspace['id']}")
    print(f"  User ID: {workspace['user_id']}")
    print(f"  Name: {workspace['name']}")
    print(f"  Current External ID: {workspace.get('aws_external_id', 'None')}")
    print()
    
    # Update external ID
    try:
        updated_external_id = update_workspace_external_id(workspace_id, new_external_id)
        if updated_external_id:
            print(f"✓ Successfully updated external ID to: {updated_external_id}")
            print()
            print("IMPORTANT: You must update the trust policy on your AWS IAM role to use this new External ID!")
            print(f"Update the Condition in your role's trust policy to:")
            print(f'  "sts:ExternalId": "{updated_external_id}"')
        else:
            print("Error: Failed to update external ID")
            sys.exit(1)
    except Exception as e:
        print(f"Error updating external ID: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
