# Error Recovery

When a tool call returns empty or errors:
- Broaden the time window (-2h, -6h, -24h)
- Simplify the query (fewer filters, broader match)
- Try a different resource type (metrics instead of logs, events instead of traces)
- Verify the service/host name exists by listing available resources

Empty results are data as they rule things out. Do not repeat a failed query unchanged. If all avenues are exhausted, state what was ruled out and stop.
