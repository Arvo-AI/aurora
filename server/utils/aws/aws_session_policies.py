"""
AWS Session Policies for restricted access modes.
These policies are applied during STS AssumeRole to further restrict permissions.

APPROACH: Allow-list for read operations to avoid AWS session policy interpretation issues.
When AWS sees a session policy, it uses it as an intersection with the base role permissions.
By explicitly allowing read operations, we ensure they work in ask mode.

SUPPORTED SERVICES:
- EC2, EKS, S3, RDS, Lambda, IAM, CloudFormation
- CloudWatch & CloudWatch Logs (for viewing logs)
- ECS, STS
"""

import json
from typing import Dict, Any

def get_read_only_session_policy() -> str:
    """
    Returns a session policy that explicitly allows read operations.

    AWS session policies work as an intersection with the base role.
    By explicitly allowing read operations, we ensure they work properly.

    Returns:
        JSON string of the session policy
    """
    policy: Dict[str, Any] = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    # EC2 read operations
                    "ec2:Describe*",
                    "ec2:Get*",
                    "ec2:List*",

                    # EKS read operations
                    "eks:Describe*",
                    "eks:List*",

                    # S3 read operations
                    "s3:Get*",
                    "s3:List*",

                    # RDS read operations
                    "rds:Describe*",
                    "rds:List*",

                    # Lambda read operations
                    "lambda:Get*",
                    "lambda:List*",

                    # IAM read operations
                    "iam:Get*",
                    "iam:List*",

                    # CloudFormation read operations
                    "cloudformation:Describe*",
                    "cloudformation:List*",
                    "cloudformation:Get*",

                    # STS identity
                    "sts:GetCallerIdentity",

                    # CloudWatch read operations
                    "cloudwatch:Describe*",
                    "cloudwatch:Get*",
                    "cloudwatch:List*",

                    # CloudWatch Logs read operations
                    "logs:Describe*",
                    "logs:Get*",
                    "logs:List*",
                    "logs:Filter*",
                    "logs:StartQuery",
                    "logs:StopQuery",
                    "logs:TestMetricFilter",

                    # ECS read operations
                    "ecs:Describe*",
                    "ecs:List*"
                ],
                "Resource": "*"
            }
        ]
    }

    # Convert to JSON with no extra whitespace to minimize size
    json_policy = json.dumps(policy, separators=(',', ':'))

    # Log the policy size for debugging
    import logging
    logger = logging.getLogger(__name__)
    logger.debug(f"Session policy size: {len(json_policy)} characters")

    return json_policy


def get_minimal_read_only_session_policy() -> str:
    """
    Returns the same session policy as the standard one.

    Returns:
        JSON string of the minimal session policy
    """
    return get_read_only_session_policy()


# Export the policy for documentation
if __name__ == "__main__":
    standard_policy = get_read_only_session_policy()
    minimal_policy = get_minimal_read_only_session_policy()

    print("Standard Read-Only Policy:")
    print(f"  Size: {len(standard_policy)} characters (limit: 2048)")
    print(f"  Under limit: {'✓' if len(standard_policy) <= 2048 else '✗'}")
    print()
    print("Minimal Read-Only Policy:")
    print(f"  Size: {len(minimal_policy)} characters (limit: 2048)")
    print(f"  Under limit: {'✓' if len(minimal_policy) <= 2048 else '✗'}")
    print()
    print("Standard policy JSON:")
    print(standard_policy)