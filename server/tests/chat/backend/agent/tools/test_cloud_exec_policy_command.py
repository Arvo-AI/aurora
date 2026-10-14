"""Policy gate must not prefix CLIs that already have their own allow rule.

`az kubectl logs` misses `^kubectl`, so Observability Only rejects logs and top.
"""

import re

from chat.backend.agent.tools.cloud_exec_tool import _command_for_policy
from utils.auth.command_policy import get_policy_templates


def _observability_allows(command: str) -> bool:
    tpl = next(t for t in get_policy_templates() if t["id"] == "observability_only")
    return any(re.search(rule["pattern"], command) for rule in tpl["allow"])


def test_azure_kubectl_logs_and_top_stay_bare_and_match_allowlist():
    logs = "kubectl logs pod-a -n app --since=3h --tail=200"
    top = "kubectl top pods -n app"
    assert _command_for_policy("azure", logs) == logs
    assert _command_for_policy("azure", top) == top
    assert _observability_allows(logs)
    assert _observability_allows(top)
    # The old prefix is what the allowlist rejected.
    assert not _observability_allows(f"az {logs}")
    assert not _observability_allows(f"az {top}")


def test_azure_kubectl_get_still_allowed():
    command = "kubectl get nodes -o wide"
    assert _command_for_policy("azure", command) == command
    assert _observability_allows(command)


def test_provider_subcommands_are_still_prefixed():
    assert _command_for_policy("azure", "vm list") == "az vm list"
    assert _command_for_policy("aws", "ecs list-clusters") == "aws ecs list-clusters"
    assert _command_for_policy("gcp", "compute instances list") == "gcloud compute instances list"
    assert _observability_allows("az vm list")
    assert _observability_allows("aws ec2 describe-instances")


def test_already_prefixed_command_is_not_doubled():
    assert _command_for_policy("azure", "az vm list") == "az vm list"
    assert _command_for_policy("aws", "aws sts get-caller-identity") == "aws sts get-caller-identity"


def test_helm_and_terraform_are_not_prefixed():
    assert _command_for_policy("azure", "helm list -A") == "helm list -A"
    assert _command_for_policy("azure", "terraform plan") == "terraform plan"
    assert _command_for_policy("aws", "kubectl logs pod-a") == "kubectl logs pod-a"


def test_sibling_clis_stay_bare_so_their_allow_rules_match():
    gsutil = "gsutil ls gs://bucket-a"
    bq = "bq ls dataset_a"
    assert _command_for_policy("gcp", gsutil) == gsutil
    assert _command_for_policy("gcp", bq) == bq
    assert _observability_allows(gsutil)
    assert _observability_allows(bq)
    # Prefixing is what made `^gsutil` miss.
    assert not _observability_allows(f"gcloud {gsutil}")

    for command in ("eksctl get cluster", "sam list endpoints", "cdk list"):
        assert _command_for_policy("aws", command) == command
    # The aws verb rule would have allowed the prefixed form.
    assert _observability_allows("aws eksctl get cluster")
    assert not _observability_allows("eksctl get cluster")


def test_prefix_matches_the_first_token_not_a_substring():
    assert _command_for_policy("azure", "azcopy list") == "az azcopy list"
    assert _command_for_policy("aws", "awslogs get") == "aws awslogs get"
