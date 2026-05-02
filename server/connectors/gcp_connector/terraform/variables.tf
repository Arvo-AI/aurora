variable "project_id" {
  description = "GCP project ID where the WIF pool and service accounts will be created"
  type        = string
}

variable "aurora_oidc_issuer" {
  description = "Aurora's OIDC issuer URL (provided on the Aurora GCP connection page)"
  type        = string
}

variable "aurora_sa_email" {
  description = "Aurora's WIF service account email (provided on the Aurora GCP connection page)"
  type        = string
}

variable "additional_project_ids" {
  description = "Additional GCP projects Aurora should have access to"
  type        = list(string)
  default     = []
}

variable "enable_read_only" {
  description = "Create a separate read-only viewer SA for Aurora's Ask mode"
  type        = bool
  default     = true
}

variable "agent_roles" {
  description = "IAM roles granted to the agent (full-access) service account"
  type        = list(string)
  default = [
    "roles/editor",
    "roles/iam.serviceAccountUser",
    "roles/bigquery.dataViewer",
  ]
}

variable "viewer_roles" {
  description = "IAM roles granted to the viewer (read-only) service account"
  type        = list(string)
  default = [
    "roles/viewer",
    "roles/logging.viewer",
    "roles/monitoring.viewer",
    "roles/browser",
    "roles/cloudasset.viewer",
    "roles/compute.viewer",
    "roles/container.viewer",
    "roles/storage.objectViewer",
  ]
}
