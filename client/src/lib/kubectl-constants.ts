// Kubectl Agent Configuration Constants
// These values should match the Helm chart defaults in kubectl-agent/chart/values.yaml

export const KUBECTL_AGENT = {
  // Default namespace (users can override with --namespace flag)
  DEFAULT_NAMESPACE: "default",
  
  // Pod labels from kubectl-agent/chart/values.yaml podLabels
  POD_LABEL_SELECTOR: "app=aurora-kubectl-agent",
  
  // Chart configuration (OCI registry)
  // Override via environment variable for custom deployments
  CHART_OCI_URL: process.env.NEXT_PUBLIC_KUBECTL_AGENT_CHART_URL || "oci://gcr.io/sublime-flux-414616/helm/aurora-kubectl-agent",
  CHART_VERSION: "1.0.3",
  RELEASE_NAME: "aurora-kubectl-agent",
  
  // Egress check configuration
  EGRESS_CHECK_IMAGE: "alpine:3.19",
  
  // UI configuration
  STORAGE_KEY: "isKubectlConnected",
  COPY_FEEDBACK_DURATION: 1200,
} as const;

