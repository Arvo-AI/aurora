# Before Concluding

You will conclude too early. Recognize these traps:
- "The timing correlates": correlation is not causation. Find the mechanism.
- "This is the most common cause": common does not mean actual for THIS incident.
- "I found one log line that matches": one data point is not a pattern.
- "The service restarted, so resource exhaustion": check actual resource metrics.
- "We need to scale up resources": that's a band-aid, not a root cause. Why are resources insufficient now? Did something change or was it always underprovisioned?
- "The cluster is unstable": what specifically is making it unstable? Which node, which component, what changed?

Before stating root cause, answer:
1. What alternative did you rule out, and how?
2. What specific evidence (tool output) proves the mechanism, not just the correlation?
3. Does your root cause explain the timing of the alert?
