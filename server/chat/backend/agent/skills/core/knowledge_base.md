KNOWLEDGE BASE (CRITICAL - CHECK FIRST FOR RUNBOOKS AND CONTEXT):
knowledge_base_search(query, limit) - Search user's uploaded documentation:
- ALWAYS search the knowledge base at the START of any investigation
- Contains runbooks, architecture docs, postmortems, and team-specific procedures
- Contains auto-discovered infrastructure topology (deployment chains, dependencies, monitoring mappings)
- Returns relevant excerpts with source file attribution
- WHEN TO SEARCH:
  1. At the START of every investigation - check for existing runbooks AND infrastructure topology
  2. When encountering unfamiliar services or systems
  3. When seeing error patterns that might match past incidents
  4. Before providing recommendations - check for documented procedures
- QUERY EXAMPLES:
  - 'payment-service deployment chain dependencies'
  - 'redis connection timeout'
  - 'what connects to database X'
  - 'escalation process database'
- IMPORTANT: Reference knowledge base findings with source citations in your analysis
- If a runbook exists for the issue, FOLLOW the documented steps
