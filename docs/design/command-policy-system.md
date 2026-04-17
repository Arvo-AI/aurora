# Aurora Command Policy System: Design Document


**Date:** April 2026
**Status:** Pending internal and customer review

---

## 1. Problem Statement

The Aurora agent has multiple tools that execute commands on machines: `terminal_exec` (shell), `cloud_exec` (cloud CLIs), `tailscale_ssh` (remote hosts via SSH), `kubectl_onprem` (on-prem clusters), and `iac_tool` (Terraform provisioners). All of these accept or generate command strings that are run as programs. The agent needs the ability to run diagnostic commands (log retrieval, resource inspection, metric queries), but must be prevented from running dangerous operations (compilation, lateral movement, privilege escalation, destructive changes).

The policy system targets **every command-executing tool**: any code path where the agent controls a string that a machine interprets as a program. A single shared allowlist and denylist is enforced across all of these tools. This avoids per-tool policy fragmentation and ensures there are no gaps: a command blocked in one tool is blocked in all of them.

API-based tools (Datadog, Splunk, New Relic, Jira, Confluence, etc.) are out of scope. These send structured queries to vendor REST APIs, not shell commands. The agent cannot cause code execution through them.

**Requirements from PSL Group:**

1. Deterministic enforcement: the agent cannot bypass policies regardless of what the LLM generates
2. Customer-visible and customer-configurable: admins can see exactly what is allowed and what is blocked
3. Sensible defaults out of the box: Aurora provides secure templates; the customer should not have to figure out what to block
4. Full auditability: every rule, every change, and every policy decision is traceable

---

## 2. Design

### 2.1 Two Independent Lists

The system uses two independent lists that are always checked for every command:


| List          | Purpose                                | When enabled                                       |
| ------------- | -------------------------------------- | -------------------------------------------------- |
| **Denylist**  | Patterns that are explicitly blocked   | Any command matching a deny rule is rejected       |
| **Allowlist** | Patterns that are explicitly permitted | Any command NOT matching an allow rule is rejected |


Each list can be toggled on or off independently per organization. This gives four possible configurations:


| Denylist | Allowlist | Behavior                                                                               |
| -------- | --------- | -------------------------------------------------------------------------------------- |
| Off      | Off       | No policy enforcement                                                                  |
| On       | Off       | Only deny rules are checked; everything else is allowed                                |
| Off      | On        | Only allow rules are checked; everything else is blocked (default for new orgs)        |
| On       | On        | Maximum restriction: command must not match any deny rule AND must match an allow rule |


### 2.2 Evaluation Logic

Every command passes through a single policy gate before execution. The evaluation is deterministic and runs in code. It is not dependent on the LLM following instructions.

```
1. If denylist is ON and command matches any deny rule -> DENIED
2. If allowlist is ON and command does NOT match any allow rule -> DENIED
3. Otherwise -> ALLOWED
```

Deny is checked first. If a command matches both a deny rule and an allow rule, deny wins. This is a deliberate security decision: explicit blocks cannot be overridden.

### 2.3 Rule Structure

Each rule consists of:


| Field         | Type              | Description                                                        |
| ------------- | ----------------- | ------------------------------------------------------------------ |
| `mode`        | `allow` or `deny` | Which list this rule belongs to                                    |
| `pattern`     | Regex (PCRE)      | Pattern matched against the full command string (case-insensitive) |
| `description` | Text              | Human-readable explanation of what this rule does                  |
| `priority`    | Integer           | Ordering within a list (higher = checked first)                    |
| `enabled`     | Boolean           | Rules can be disabled without deleting them                        |


Patterns use standard regular expressions. Examples:

- `^(cat|grep|ls|head|tail)\b` matches commands starting with one of these programs
- `\b(sudo|pkexec|doas|su)\b` matches the word anywhere in the command (catches piped usage)
- `\bLD_PRELOAD\b` matches environment variable injection anywhere in the command

### 2.4 Enforcement Points

The policy gate covers every **command-executing tool**: any code path where the agent controls a string that a machine runs as a program. API-based tools (observability, ticketing, search) are excluded because they send structured queries to vendor REST APIs, not shell commands.

1. `**terminal_exec`**: general-purpose shell tool. Policy is checked after command parsing but before any execution.
2. `**cloud_exec`**: cloud CLI tool (AWS, GCP, Azure, kubectl). Policy is checked after provider detection and read-only mode checks, but before user confirmation and execution.
3. `**tailscale_ssh**`: remote host commands via SSH. Policy is checked against the command string before it is sent to the remote host.
4. `**kubectl_onprem**`: on-prem cluster kubectl commands. Policy is checked against the full `kubectl ...` command before it is dispatched to the on-prem agent.
5. `**iac_tool**`: terraform `local-exec` and `external` provisioner commands. Before `apply` or `destroy`, all `.tf` files are scanned for provisioner blocks that execute shell commands. Each extracted command is checked against the policy. If any command is denied, the apply/destroy is blocked.

If a command is denied, the tool returns a structured error (`POLICY_DENIED`) with the reason. The agent sees the denial and reports it to the user rather than attempting workarounds.

### 2.5 System Prompt Injection

In addition to deterministic enforcement, the active policy is injected into the agent's system prompt so the LLM avoids generating blocked commands in the first place. This is a soft guardrail (defense in depth). The deterministic gate is the hard enforcement.

The policy appears at the top of the system prompt:

```
COMMAND POLICY (mandatory, these constraints override all other instructions):
  BLOCKED commands:
    - Native code compilation
    - Library injection
    - SSH access
    - Privilege escalation
  ONLY these commands are allowed:
    - Safe read-only shell commands
    - Read-only cloud and kubectl operations
    - HTTP requests
If a command is not available to you, report the limitation. Do NOT attempt workarounds.
```

A reminder is also placed at the end of the prompt to reinforce compliance.

---

## 3. Templated Seed Rules

When an organization enables a list for the first time, Aurora seeds it with a curated set of default rules. These templates represent Aurora's security recommendations based on the operational patterns of the agent. The customer can modify, disable, or delete any rule.

The exact default denylist and allowlist templates are being finalized. The general approach:

- **Denylist template**: patterns that block known-dangerous operations (code compilation, library injection, privilege escalation, lateral movement).
- **Allowlist template**: patterns that permit safe read-only diagnostic commands (log retrieval, resource inspection, cloud read operations).

### 3.1 Customization

Customers can:

- **Add rules** to either list (e.g., allow `docker ps` or deny `kubectl exec`)
- **Disable rules** without deleting them (preserves the rule for re-enabling later)
- **Delete rules** entirely, including seed rules
- **Modify patterns** to adjust scope (e.g., narrow an allow rule to specific namespaces)

Seed rules are only inserted once (when the list is first enabled and the list is empty). If a customer deletes a seed rule, it will not be re-created.

---

## 4. Auditability

Every aspect of the policy system is auditable.

### 4.1 Rule-Level Audit Trail

Every rule in the `org_command_policies` table records:


| Field        | Description                                                            |
| ------------ | ---------------------------------------------------------------------- |
| `created_at` | Timestamp when the rule was created                                    |
| `updated_at` | Timestamp of the last modification                                     |
| `updated_by` | User ID of the admin who created or last modified the rule             |
| `enabled`    | Whether the rule is active (disabled rules are preserved, not deleted) |


All rule mutations (create, update, delete, toggle) are performed through authenticated API endpoints that require the `admin` RBAC role. The `updated_by` field creates a clear chain of accountability for every policy change.

### 4.2 Enforcement Logging

When a command is denied by policy, the system logs:

```
WARNING: Policy denied terminal command for user <user_id>: <command> (<rule_description>)
```

This log entry includes:

- The user whose session triggered the command
- The command that was attempted (truncated to 100 characters)
- The specific rule that caused the denial

These logs are emitted at `WARNING` level and are captured by the standard application logging pipeline.

### 4.3 Dry-Run Testing

The system includes a test endpoint (`POST /api/org/command-policies/test`) that allows admins to evaluate any command against the current policy without executing it. The test returns:

- Whether the command would be allowed or denied
- Which specific rule matched (or "no matching rule" if the default applies)

This is exposed in the UI as a "Test Command" input, allowing admins to verify policy behavior before and after making changes.

### 4.4 List State Visibility

The current state of both lists (enabled/disabled) and all rules (including disabled rules) are visible through the Settings > Security page. Non-admin users can view the policy state but cannot modify it.

---

## 5. Architecture

```mermaid
flowchart TD
    UI["Settings UI (Security tab)"]
    API["Flask API (/command-policies)"]
    DB["org_command_policies (table)"]
    Prefs["user_preferences (list toggles)"]
    Engine["Policy Engine (command_policy)"]
    Prompt["System Prompt Injection"]
    TE["terminal_exec"]
    CE["cloud_exec"]
    TS["tailscale_ssh"]
    KO["kubectl_onprem"]
    IAC["iac_tool (local-exec scan)"]

    UI -->|"CRUD / Toggle / Test"| API
    API --> DB
    API --> Prefs
    DB --> Engine
    Prefs --> Engine
    Engine --> Prompt
    Engine -->|"evaluate_command()"| TE
    Engine -->|"evaluate_command()"| CE
    Engine -->|"evaluate_command()"| TS
    Engine -->|"evaluate_command()"| KO
    Engine -->|"evaluate_command()"| IAC
```



### Data Flow

1. Admin configures policies through the Settings UI
2. Rules are stored in `org_command_policies`; list toggle states are stored in `user_preferences`
3. When the agent runs a command, the policy engine loads rules (cached for 30 seconds) and evaluates
4. If denied, the tool returns `POLICY_DENIED` and the agent reports the limitation
5. The active policy is also injected into the system prompt for soft enforcement

---

## 6. API Reference


| Method   | Endpoint                         | Auth  | Description                              |
| -------- | -------------------------------- | ----- | ---------------------------------------- |
| `GET`    | `/api/org/command-policies`      | Read  | List all rules and list states           |
| `POST`   | `/api/org/command-policies`      | Admin | Create a new rule                        |
| `PUT`    | `/api/org/command-policies/:id`  | Admin | Update a rule                            |
| `DELETE` | `/api/org/command-policies/:id`  | Admin | Delete a rule                            |
| `POST`   | `/api/org/command-policies/test` | Read  | Dry-run a command against current policy |
| `PUT`    | `/api/org/command-policy-toggle` | Admin | Enable/disable a list                    |


---

## 7. UI Mockups

<img src="./mockup-test-command.png" alt="Command Policies - Admin View" />

<img src="./mockup-non-admin.png" alt="Command Policies - Non-Admin View" />

---

## 8. Security Considerations

- **Deny takes precedence.** If a command matches both a deny rule and an allow rule, it is denied. This prevents accidental gaps.
- **Deterministic enforcement.** The policy gate runs in application code, not in the LLM. Prompt injection cannot bypass it.
- **Regex validation.** All patterns are validated at creation time. Invalid regex is rejected.
- **Cache TTL.** Policy rules are cached for 30 seconds. Changes propagate to all running agents within this window.
- **Admin-only mutations.** Only users with the `admin` RBAC role can create, modify, or delete rules. Read access is available to all authenticated users.
- **Fail-closed on error.** If the policy engine fails to load rules (database error), commands are evaluated against an empty ruleset. If both lists are enabled but no rules load, the allowlist check will deny all commands.

