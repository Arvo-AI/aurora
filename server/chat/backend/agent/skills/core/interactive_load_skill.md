ON-DEMAND SKILL LOADING:
- Integration skills load on demand. The CONNECTED INTEGRATIONS index lists what is available; it is a directory, not a checklist.
- Decide which skills you actually need based on the task at hand. A CPU/memory alert almost always means inspect the VM, pod, or node first — you already know kubectl, gcloud, aws, and az commands; do not load Jira/Notion/Confluence unless something in the alert points to a human-tracked issue.
- When you do need an integration's specific workflow (e.g. datadog query syntax, github_rca arguments, splunk SPL idioms), call `load_skill('integration_id')` first to fetch its workflow.
- You only need to load a skill once per conversation; the guidance stays in context after loading.
