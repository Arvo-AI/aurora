THIS ALERT WAS OPENED BY AURORA WATCH, NOT BY A MONITORING RULE:
Aurora compared this cluster with its own learned baseline and saw a regression that no alert rule reported. The payload holds:
- `findings`: what regressed and when, with evidence (pod states, Service endpoints, Warning events, sample log lines)
- `suspected_changes`: cluster changes ranked by how the changed object relates to the failing one and how close in time the change was

Start from the top suspected change. Confirm or refute it against the cluster: read the object's current spec, its rollout history or ReplicaSet revisions, the ConfigMap it points to, and connect it to each finding's evidence. When it explains every finding, say so and do not chase unrelated hypotheses. When it does not, name the finding it leaves unexplained and investigate that one. Some findings are side effects of another (clients timing out on a Service with no endpoints): say which is the cause and which is the symptom. If no suspected change is listed, the cause is probably outside the cluster (a dependency, a node, a cloud service): check those.

End your report with one line: `Suspected change: confirmed`, `Suspected change: likely`, or `Suspected change: refuted`, followed by the object.
