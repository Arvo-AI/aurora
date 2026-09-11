from dataclasses import dataclass


@dataclass
class PromptSegments:
    system_invariant: str
    provider_constraints: str
    regional_rules: str
    ephemeral_rules: str
    long_documents_note: str
    provider_context: str
    prerequisite_checks: str
    terraform_validation: str
    model_overlay: str
    failure_recovery: str
    manual_vm_access: str = ""  # Manual VM access hints with managed keys
    background_mode: str = ""  # Background chat autonomous operation instructions
    knowledge_base_memory: str = ""  # Org memory index (context, runbooks, infra, learnings)
    integration_index: str = ""  # Skills-based: compact index of connected integrations
    security_policy: str = ""  # Org-level command allow/deny policy
    is_rca_background: bool = False  # True when prompt is for a background RCA (not action)
