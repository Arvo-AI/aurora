output "wif_config" {
  description = "Paste these values into the Aurora GCP connection page"
  value = {
    project_id             = var.project_id
    project_number         = data.google_project.current.number
    pool_id                = google_iam_workload_identity_pool.aurora.workload_identity_pool_id
    provider_id            = google_iam_workload_identity_pool_provider.aurora.workload_identity_pool_provider_id
    sa_email               = google_service_account.aurora_agent.email
    viewer_sa_email        = var.enable_read_only ? google_service_account.aurora_viewer[0].email : null
    accessible_project_ids = local.all_project_ids
  }
}
