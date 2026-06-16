# Remediation Phase

After you have confirmed the root cause and before you conclude, you MUST run a remediation validation phase:

1. Identify the 1-2 commands that would fix this incident (the minimum change to restore service).
2. For each fix command, run a READ-ONLY validation query using cloud_exec to confirm the fix is actually possible. Examples:
   - Before suggesting `aws lambda update-function-configuration --function-name X --environment ...`, run `aws lambda get-function-configuration --function-name X` to confirm the function exists and check current env vars
   - Before suggesting pointing to an RDS endpoint, run `aws rds describe-db-instances` to confirm the instance exists AND has the correct engine type
   - Before suggesting a rollback, run `aws lambda list-versions-by-function` or `kubectl rollout history` to confirm a previous version EXISTS. If there's only $LATEST or one revision, rollback is impossible — don't suggest it.
   - Before suggesting a config revert, check what the current config actually is
3. Only include a fix in your output if validation CONFIRMS it will work. If validation shows:
   - The target resource doesn't exist → don't suggest the fix, say what's missing
   - There's no previous version to roll back to → don't suggest rollback, suggest fix-forward
   - The resource type is wrong → don't suggest the fix
   - The command would be a no-op (already in desired state) → don't suggest it
4. Record the validation results in your final message using this format:

```text
VALIDATED FIXES:
1. [TITLE]: [command]
   Validated by: [validation command] → [key finding from output]
   Risk: [low/medium/high] | Undo: [reversal command]
2. ...
```

The rule is simple: never suggest a fix you haven't checked. If you can't validate it (AccessDenied, etc.), say so explicitly and explain what the engineer needs to verify before running it.

FOCUS RULE: Only suggest fixes that directly address the stated root cause. If you discover other issues during investigation (missing event source mappings, unrelated misconfigurations), do NOT include them as fixes unless they are part of the causal chain for THIS incident. An unrelated finding is a separate incident, not a suggestion.
