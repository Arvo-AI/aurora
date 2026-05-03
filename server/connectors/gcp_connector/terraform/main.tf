terraform {
  required_version = ">= 1.3"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 4.0"
    }
  }
}

locals {
  all_project_ids = concat([var.project_id], var.additional_project_ids)
  use_org_binding = var.org_id != ""

  required_apis = [
    "compute.googleapis.com",
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudbilling.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "bigquery.googleapis.com",
    "sqladmin.googleapis.com",
    "appengine.googleapis.com",
    "pubsub.googleapis.com",
    "dns.googleapis.com",
    "cloudfunctions.googleapis.com",
    "firestore.googleapis.com",
    "dataflow.googleapis.com",
    "redis.googleapis.com",
    "endpoints.googleapis.com",
    "composer.googleapis.com",
    "containerregistry.googleapis.com",
    "cloudasset.googleapis.com",
    "sts.googleapis.com",
  ]
}

# ---------------------------------------------------------------------------
# Enable required APIs on the primary project
# ---------------------------------------------------------------------------
resource "google_project_service" "apis" {
  for_each                   = toset(local.required_apis)
  project                    = var.project_id
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}

# ---------------------------------------------------------------------------
# Workload Identity Pool + OIDC Provider
#
# The issuer is https://accounts.google.com because Aurora authenticates
# with a Google-signed ID token (from its own GCP SA). The attribute
# condition restricts federation to Aurora's specific SA email.
# ---------------------------------------------------------------------------
resource "google_iam_workload_identity_pool" "aurora" {
  project                   = var.project_id
  workload_identity_pool_id = "aurora-wif-pool"
  display_name              = "Aurora WIF Pool"
  description               = "Allows Aurora SaaS to federate into this project"

  depends_on = [google_project_service.apis["sts.googleapis.com"]]
}

resource "google_iam_workload_identity_pool_provider" "aurora" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.aurora.workload_identity_pool_id
  workload_identity_pool_provider_id = "aurora-provider"
  display_name                       = "Aurora OIDC Provider"

  oidc {
    issuer_uri = "https://accounts.google.com"
  }

  attribute_mapping = {
    "google.subject" = "assertion.sub"
    "attribute.email" = "assertion.email"
  }
  attribute_condition = "attribute.email == \"${var.aurora_sa_email}\""
}

# ---------------------------------------------------------------------------
# Agent (full-access) Service Account
# ---------------------------------------------------------------------------
resource "google_service_account" "aurora_agent" {
  project      = var.project_id
  account_id   = "aurora-agent"
  display_name = "Aurora Agent"
  description  = "Full-access SA used by Aurora in Agent mode"
}

resource "google_service_account_iam_member" "agent_wif_binding" {
  service_account_id = google_service_account.aurora_agent.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.aurora.name}/*"
}

resource "google_project_iam_member" "agent_roles" {
  for_each = local.use_org_binding ? {} : {
    for pair in setproduct(local.all_project_ids, var.agent_roles) :
    "${pair[0]}:${pair[1]}" => { project = pair[0], role = pair[1] }
  }
  project = each.value.project
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.aurora_agent.email}"
}

resource "google_organization_iam_member" "agent_org_roles" {
  for_each = local.use_org_binding ? toset(concat(
    var.agent_roles,
    ["roles/resourcemanager.organizationViewer"],
  )) : toset([])
  org_id = var.org_id
  role   = each.value
  member = "serviceAccount:${google_service_account.aurora_agent.email}"
}

# ---------------------------------------------------------------------------
# Viewer (read-only) Service Account
# ---------------------------------------------------------------------------
resource "google_service_account" "aurora_viewer" {
  count        = var.enable_read_only ? 1 : 0
  project      = var.project_id
  account_id   = "aurora-viewer"
  display_name = "Aurora Viewer"
  description  = "Read-only SA used by Aurora in Ask mode"
}

resource "google_service_account_iam_member" "viewer_wif_binding" {
  count              = var.enable_read_only ? 1 : 0
  service_account_id = google_service_account.aurora_viewer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.aurora.name}/*"
}

resource "google_project_iam_member" "viewer_roles" {
  for_each = (var.enable_read_only && !local.use_org_binding) ? {
    for pair in setproduct(local.all_project_ids, var.viewer_roles) :
    "${pair[0]}:${pair[1]}" => { project = pair[0], role = pair[1] }
  } : {}
  project = each.value.project
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.aurora_viewer[0].email}"
}

resource "google_organization_iam_member" "viewer_org_roles" {
  for_each = (var.enable_read_only && local.use_org_binding) ? toset(var.viewer_roles) : toset([])
  org_id   = var.org_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.aurora_viewer[0].email}"
}
