MANDATORY INVESTIGATION STEPS - DO NOT STOP UNTIL ALL ARE DONE:
1. List resources from EVERY provider: {providers_display}
2. If SSH keys are already available in ~/.ssh/, SSH into an affected VM for system-level diagnostics. If SSH is unavailable or access is denied, use cloud provider APIs and monitoring tools instead - do NOT attempt to generate keys or bypass access controls.
3. Check system metrics via available tools (cloud monitoring APIs, kubectl top, or SSH if accessible)
4. Check logs: cloud logging, kubectl logs, monitoring integrations, or SSH if accessible
5. Identify root cause with evidence
6. Provide remediation steps

YOU MUST make 15-20+ tool calls. After EACH tool call, continue investigating.
