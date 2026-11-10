# The console as a Cloud Run service behind Cloud Run's own IAP: no load balancer. It
# scales to zero when idle, and the app verifies IAP's JWT against the service's audience.
#
# IAM is not managed here beyond the service. The service account needs roles/workflows.invoker
# and workflows.viewer, roles/run.developer (cancel task executions), roles/cloudscheduler.admin
# (pause), roles/logging.viewer and logWriter (its audit log), roles/aiplatform.user (model
# aliases, deleting retired resources), roles/bigquery.dataViewer and jobUser (Catalog), roles/datalineage.viewer (lineage),
# roles/bigquery.resourceViewer (a task's BigQuery jobs run as other accounts) and read on the buckets; for notebooks, object admin on the workspace prefix and Vertex AI's notebook runtime
# permissions (list, assign, start, stop, delete); the applying account needs roles/run.admin, since IAP's service agent
# gets roles/run.invoker on the service.
data "google_project" "this" {
  project_id = var.gcp_project
}

locals {
  audience = "/projects/${data.google_project.this.number}/locations/${var.region}/services/${var.name}"
}

resource "google_cloud_run_v2_service" "console" {
  project             = var.gcp_project
  location            = var.region
  name                = var.name
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = false

  # The service's own cap, across every revision taking traffic, beside the template's per-revision one.
  scaling {
    max_instance_count = var.max_instances
  }

  template {
    service_account = var.service_account
    # A notebook cell run from the editor streams its outputs for as long as it runs: Cloud Run's
    # default 300 s cut every longer cell off from the page that ran it.
    timeout = "3600s"
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }
    containers {
      image = var.image
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = true
      }
      dynamic "env" {
        for_each = merge(var.env, {
          MLOPS_PROJECT      = var.gcp_project
          MLOPS_REGION       = var.region
          MLOPS_IAP_AUDIENCE = local.audience
        })
        content {
          name  = env.key
          value = env.value
        }
      }
      # By key: a for_each can't range over a sensitive map's values.
      dynamic "env" {
        for_each = nonsensitive(toset(keys(var.secret_env)))
        content {
          name  = env.value
          value = var.secret_env[env.value]
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "iap" {
  project  = var.gcp_project
  location = var.region
  name     = google_cloud_run_v2_service.console.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

resource "google_iap_web_cloud_run_service_iam_member" "access" {
  for_each = toset(var.iap_members)

  project                = var.gcp_project
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.console.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}
