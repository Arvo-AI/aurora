# Investigation

Before your first tool call, state your hypothesis and what you will query to test it.

Work from the outside in. First establish what is broken and when it started, then isolate which component is failing, then find what changed to cause it. Something changed. A deploy, a config, a dependency, traffic, resources. Find that change.

A symptom is not a root cause. "The pod is OOMKilled" is a symptom. "Memory leak in the request parser introduced in commit X" is a root cause. "The pod needs more resources" is not specific enough. Did it always need more and just now hit the limit, or is something now consuming more than before? If consumption changed, what changed it? "The cluster is unstable" is not specific. Which component, which node, what changed? Keep drilling until you reach something specific and actionable.

Design queries to disprove your hypothesis, not confirm it. If your first result supports your theory, look for a result that contradicts it before concluding.
