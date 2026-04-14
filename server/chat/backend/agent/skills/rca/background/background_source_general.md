MANDATORY INVESTIGATION STEPS - DO NOT STOP UNTIL ALL ARE DONE:
1. List resources from EVERY provider: {providers_display}
2. SSH into at least one affected VM (use OpenSSH terminal_exec above)
3. Check system metrics: top, free -m, df -h, dmesg | tail
4. Check logs: journalctl, /var/log/, cloud logging
5. Identify root cause with evidence
6. Provide remediation steps

YOU MUST make 15-20+ tool calls. After EACH tool call, continue investigating.
