# A bundle's jobs for one target, from `mlops bundle render -t <target>`: one Workflow per
# job (workflows/job.yaml runs its tasks) and its definition object, the runner Workflows
# some tasks need (vertex_pipeline, batch_serve, trigger, watchdog), one Cloud Run job per job
# cluster plus the dbt job, one Cloud Scheduler entry per scheduled job and per triggered
# job, and one log-based alert per notified event. Every run's argument is the plan's,
# already resolved in Python (gcpbricks.bundle.job_argument), so `mlops bundle run`
# and the scheduler send the same. The bundle's experiments, models and endpoints are
# `mlops bundle deploy`'s, not this module's.
#
# IAM is not managed here. The run_as service account needs roles/workflows.invoker (the
# scheduler starts executions as it, a job's Workflow starts its runners as it, and another
# bundle's run_job_task needs it on the jobs it calls),
# roles/run.developer on the Cloud Run jobs, BigQuery and the bucket for what its tasks
# touch, and for a file_arrival trigger roles/eventarc.eventReceiver (the project's GCS
# service agent also needs roles/pubsub.publisher); the applying account needs roles/workflows.editor, roles/cloudscheduler.admin,
# roles/run.admin and roles/iam.serviceAccountUser on the run_as account.
locals {
  jobs             = var.enabled ? var.plan.jobs : {}
  task_workflows   = var.enabled ? var.plan.task_workflows : {}
  run_jobs         = var.enabled ? var.plan.run_jobs : {}
  scheduled        = { for name, job in local.jobs : name => job if job.schedule != null }
  needs_dbt_image  = anytrue([for rj in values(local.run_jobs) : rj.image == null])
  alerted_workflow = { for name, job in local.jobs : name => job.workflow }
  triggers         = var.enabled ? var.plan.triggers : {}
  file_triggers    = { for k, t in local.triggers : k => t if t.file_bucket != null }
  alerts = var.enabled ? {
    for a in var.plan.alerts : a.key => merge(a, {
      channels = concat(a.channels, a.job_failure ? var.notification_channels : [])
    })
  } : {}
  emails     = toset(flatten([for a in values(local.alerts) : a.emails]))
  schemas    = var.enabled ? var.plan.schemas : {}
  dashboards = var.enabled ? var.plan.dashboards : {}
  notebooks  = var.enabled ? var.plan.notebooks : {}
}

# A schema's dataset. A destroy never takes its tables with it: a non-empty dataset
# fails to delete instead. Description and labels are set only at creation, so adopting an
# existing dataset needs no bigquery.datasets.update.
resource "google_bigquery_dataset" "this" {
  for_each = local.schemas

  project                    = each.value.project
  dataset_id                 = each.value.dataset_id
  location                   = each.value.location
  description                = each.value.description
  labels                     = each.value.labels
  delete_contents_on_destroy = false

  lifecycle {
    ignore_changes = [description, labels]
  }
}

# A dashboard's spec, which the console renders (gcpbricks.dashboards).
resource "google_storage_bucket_object" "dashboard" {
  for_each = local.dashboards

  bucket       = var.plan.bucket
  name         = each.value.object
  content      = each.value.content
  content_type = "application/json"
}

resource "google_storage_bucket_object" "notebook" {
  for_each = local.notebooks

  bucket       = var.plan.bucket
  name         = each.value.object
  content      = each.value.content
  content_type = "application/x-ipynb+json"
}

resource "google_storage_bucket_object" "definition" {
  for_each = local.jobs

  bucket       = var.plan.bucket
  name         = each.value.definition_object
  content      = each.value.definition
  content_type = "application/json"
}

resource "google_workflows_workflow" "job" {
  for_each = local.jobs

  name            = each.value.workflow
  project         = var.gcp_project
  region          = var.plan.region
  description     = "${var.plan.bundle}: ${each.value.description} (gcpbricks)."
  service_account = var.plan.service_account
  source_contents = file("${path.module}/workflows/job.yaml")
  call_log_level  = "LOG_ERRORS_ONLY"
  # A job dropped from the bundle stays, retired (bundle.retired_jobs), until an admin deletes
  # it from the console: the apply after that deletes what's left of it.
  deletion_protection = false
  labels              = each.value.labels
  # Where the job's definition is: so a job starts with no argument, whoever starts it.
  user_env_vars = each.value.env
}

resource "google_workflows_workflow" "task_runner" {
  for_each = local.task_workflows

  name            = each.value
  project         = var.gcp_project
  region          = var.plan.region
  description     = "${var.plan.bundle}: runs its ${each.key} tasks (gcpbricks)."
  service_account = var.plan.service_account
  source_contents = file("${path.module}/workflows/${each.key}.yaml")
  call_log_level  = "LOG_ERRORS_ONLY"
  # Deleted by the apply after no job, declared or retired, has a task it runs.
  deletion_protection = false
}

resource "google_cloud_run_v2_job" "this" {
  for_each = local.run_jobs

  name                = each.key
  project             = var.gcp_project
  location            = var.plan.region
  deletion_protection = false
  labels              = each.value.labels

  template {
    task_count  = each.value.tasks
    parallelism = each.value.tasks
    template {
      service_account = var.plan.service_account
      # A task's own retries are the Workflow's: Cloud Run never reruns one by itself.
      max_retries = 0
      timeout     = "86400s"

      containers {
        image = coalesce(each.value.image, var.dbt_image)
        args  = each.value.args

        dynamic "env" {
          for_each = each.value.env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "resources" {
          for_each = each.value.cpu == null ? [] : [each.value]
          content {
            limits = { cpu = resources.value.cpu, memory = resources.value.memory }
          }
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = !local.needs_dbt_image || var.dbt_image != ""
      error_message = "The plan has dbt_tasks without a job cluster: set dbt_image to the platform's dbt image."
    }
  }
}

resource "google_cloud_scheduler_job" "this" {
  for_each = local.scheduled

  name      = "${var.plan.bundle}-${replace(each.key, "_", "-")}-${var.plan.target}"
  project   = var.gcp_project
  region    = var.plan.region
  schedule  = each.value.schedule
  time_zone = each.value.time_zone
  paused    = each.value.paused
  # A retry would start a second run; a failed run is for a person to look at.
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://workflowexecutions.googleapis.com/v1/${google_workflows_workflow.job[each.key].id}/executions"
    body        = base64encode(jsonencode({ argument = each.value.argument }))
    headers     = { "Content-Type" = "application/json" }

    oauth_token {
      service_account_email = var.plan.service_account
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
}

# A table_update / file_arrival trigger: the trigger Workflow checks it on a short cron.
resource "google_cloud_scheduler_job" "trigger" {
  for_each = local.triggers

  name      = "${var.plan.bundle}-${replace(each.key, "_", "-")}-trigger-${var.plan.target}"
  project   = var.gcp_project
  region    = var.plan.region
  schedule  = each.value.schedule
  time_zone = "UTC"
  paused    = each.value.paused
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://workflowexecutions.googleapis.com/v1/${google_workflows_workflow.task_runner["trigger"].id}/executions"
    body        = base64encode(jsonencode({ argument = each.value.argument }))
    headers     = { "Content-Type" = "application/json" }

    oauth_token {
      service_account_email = var.plan.service_account
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
}

# A file_arrival trigger is also checked at once on each object written to its bucket:
# Eventarc starts the job's wake Workflow, which runs the trigger Workflow. Eventarc's
# trigger lives in the bucket's location; the poll above still covers a lost event.
data "google_storage_bucket" "watched" {
  for_each = toset([for t in values(local.file_triggers) : t.file_bucket])

  name = each.value
}

resource "google_workflows_workflow" "wake" {
  for_each = local.file_triggers

  name            = each.value.wake_workflow
  project         = var.gcp_project
  region          = var.plan.region
  description     = "${var.plan.bundle}: wakes ${each.key}'s file_arrival trigger (gcpbricks)."
  service_account = var.plan.service_account
  source_contents = file("${path.module}/workflows/wake.yaml")
  call_log_level  = "LOG_ERRORS_ONLY"
  # A job dropped from the bundle has its Workflow deleted by the next apply.
  deletion_protection = false
  user_env_vars = {
    MLOPS_TRIGGER_ARGUMENT = each.value.argument
    MLOPS_TRIGGER_WORKFLOW = var.plan.task_workflows["trigger"]
  }
}

resource "google_eventarc_trigger" "file" {
  for_each = local.file_triggers

  name            = "${var.plan.bundle}-${replace(each.key, "_", "-")}-files-${var.plan.target}"
  project         = var.gcp_project
  location        = lower(data.google_storage_bucket.watched[each.value.file_bucket].location)
  service_account = var.plan.service_account

  matching_criteria {
    attribute = "type"
    value     = "google.cloud.storage.object.v1.finalized"
  }
  matching_criteria {
    attribute = "bucket"
    value     = each.value.file_bucket
  }

  destination {
    workflow = google_workflows_workflow.wake[each.key].id
  }
}

# Every job execution's logs, including its mlops_run and mlops_task entries, into a
# longer-lived log bucket.
resource "google_logging_project_sink" "this" {
  for_each = var.log_bucket == "" ? {} : local.alerted_workflow

  project                = var.gcp_project
  name                   = each.value
  destination            = "logging.googleapis.com/${var.log_bucket}"
  unique_writer_identity = true
  filter                 = "resource.type=\"workflows.googleapis.com/Workflow\" AND resource.labels.workflow_id=\"${each.value}\""
}

resource "google_monitoring_notification_channel" "email" {
  for_each = local.emails

  project      = var.gcp_project
  display_name = "${var.plan.bundle} (${var.plan.target}): ${each.value}"
  type         = "email"
  labels       = { email_address = each.value }
}

# One per notified event (bundle._alerts): a log-based alert on the mlops_run / mlops_task
# entries the Workflows write.
resource "google_monitoring_alert_policy" "event" {
  for_each = { for k, a in local.alerts : k => a if length(a.emails) + length(a.channels) > 0 }

  project      = var.gcp_project
  display_name = "${var.plan.bundle} ${each.key} (${var.plan.target})"
  combiner     = "OR"

  conditions {
    display_name = each.key
    condition_matched_log {
      filter = each.value.filter
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  notification_channels = concat(
    [for e in each.value.emails : google_monitoring_notification_channel.email[e].id],
    each.value.channels,
  )

  documentation {
    mime_type = "text/markdown"
    content   = "${var.plan.bundle} ${each.key} on ${var.plan.target}. `mlops bundle runs <job> -t ${var.plan.target}` lists recent runs; the log entry names the job, task and state."
  }
}
