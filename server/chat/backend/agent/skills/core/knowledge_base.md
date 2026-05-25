KNOWLEDGE BASE:
knowledge_base_search(query, limit) - Search user's uploaded documentation:
- The knowledge base contains runbooks, past incidents, and topology info. Search it when prior knowledge would help. For alerts that are self-explanatory from the payload, you can skip ahead.
- Contains runbooks, architecture docs, postmortems, and team-specific procedures
- Contains auto-discovered infrastructure topology (deployment chains, dependencies, monitoring mappings)
- Returns relevant excerpts with source file attribution
- WHEN TO SEARCH:
  1. When encountering unfamiliar services or systems
  2. When seeing error patterns that might match past incidents
  3. Before providing recommendations - check for documented procedures
- QUERY EXAMPLES:
  - 'payment-service deployment chain dependencies'
  - 'redis connection timeout'
  - 'what connects to database X'
  - 'escalation process database'
- IMPORTANT: Reference knowledge base findings with source citations in your analysis
- If a runbook exists for the issue, FOLLOW the documented steps
