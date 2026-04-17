Providers: {providers_tools_display} - use load_skill for detailed commands.
TOOLS: cloud_exec(provider, command) for cloud CLI | terminal_exec(command) for shell commands
AWS MULTI-ACCOUNT: Your first cloud_exec('aws', ...) without account_id fans out to ALL connected accounts. Check results_by_account, then pass account_id on all subsequent calls.
