"""Startup Check — the repo check-engine light.

A read-only, zero-write whole-repo scan that surfaces the *preconditions of
known incident classes* before they reach a cluster. Two layers:

- Layer 1 (shipped baseline): deterministic rules derived from the fault
  catalog Aurora's own lab has empirically shown cause outages
  (github.com/Arvo-AI/aurora-lab, scenarios 01-21). They only fire on
  provable in-repo inconsistencies, keeping the false-alarm rate low —
  a check-engine light that fires weekly gets ignored.
- Layer 2 (user context): optional facts about the target environment
  (node sizes, measured memory usage, StorageClasses, cluster domain)
  that sharpen checks static files cannot answer alone.

Output reuses the change-gating verdict shape (SAFE|RISKY + findings with
severity/file/line), so the risk map renders through the same muscle.
"""
