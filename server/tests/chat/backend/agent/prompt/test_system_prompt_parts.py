"""The cache breakpoint sits between the two parts, so anything per session leaking
into the static part would turn every shared-prefix cache read into a write."""

from chat.backend.agent.prompt.composer import (
    assemble_system_prompt,
    assemble_system_prompt_parts,
)
from chat.backend.agent.prompt.schema import PromptSegments


def _segments(**overrides) -> PromptSegments:
    base = dict(
        system_invariant="INVARIANT",
        provider_constraints="PROVIDERS",
        regional_rules="REGIONS",
        ephemeral_rules="MODE",
        long_documents_note="LONGDOC",
        provider_context="PCTX",
        prerequisite_checks="PREREQ",
        terraform_validation="TF",
        model_overlay="OVERLAY",
        failure_recovery="RECOVERY",
        background_mode="ALERT",
        knowledge_base_memory="MEMORY",
        integration_index="SKILLS",
        security_policy="POLICY",
    )
    base.update(overrides)
    return PromptSegments(**base)


def test_static_part_holds_only_cross_session_segments():
    static, dynamic = assemble_system_prompt_parts(_segments())
    assert static == "INVARIANT\nPROVIDERS\nREGIONS"
    for per_session in ("POLICY", "ALERT", "MEMORY", "MODE", "LONGDOC", "SKILLS"):
        assert per_session in dynamic and per_session not in static


def test_static_part_ignores_per_session_changes():
    a, _ = assemble_system_prompt_parts(_segments(security_policy="org A", knowledge_base_memory="3d ago"))
    b, _ = assemble_system_prompt_parts(_segments(security_policy="org B", knowledge_base_memory="4d ago"))
    assert a == b


def test_policy_reminder_follows_the_policy():
    _, with_policy = assemble_system_prompt_parts(_segments())
    _, without = assemble_system_prompt_parts(_segments(security_policy=""))
    assert with_policy.endswith("Do not attempt workarounds.")
    assert "REMINDER" not in without


def test_rca_drops_terraform_and_recovery():
    _, dynamic = assemble_system_prompt_parts(_segments(is_rca_background=True))
    assert "TF" not in dynamic and "RECOVERY" not in dynamic


def test_joined_prompt_is_static_then_dynamic():
    static, dynamic = assemble_system_prompt_parts(_segments())
    assert assemble_system_prompt(_segments()) == f"{static}\n{dynamic}"
