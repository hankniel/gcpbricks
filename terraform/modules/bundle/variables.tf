variable "plan" {
  description = "`mlops bundle render -t <target>` output, e.g. jsondecode(file(\"$${path.root}/.mlops/prod.json\"))."
  type = object({
    bundle          = string
    target          = string
    region          = string
    service_account = string
    bucket          = string
    jobs = map(object({
      workflow          = string
      description       = string
      schedule          = optional(string)
      time_zone         = string
      paused            = bool
      labels            = map(string)
      definition_object = string
      # The job Workflow's environment: bundle.job_env.
      env = map(string)
      # JSON text: the job's definition, uploaded to definition_object.
      definition = string
      # JSON text: the Workflow's argument, sent as is.
      argument = string
    }))
    run_jobs = map(object({
      # null: the platform's dbt image, var.dbt_image.
      image  = optional(string)
      env    = map(string)
      cpu    = optional(string)
      memory = optional(string)
      labels = map(string)
      # Parallel tasks (a job cluster's num_workers); null: Cloud Run's one.
      tasks = optional(number)
      # The container's command; null: the image's own (tasks pass theirs per execution).
      args = optional(list(string))
    }))
    task_workflows = map(string)
    triggers = map(object({
      schedule = string
      paused   = bool
      argument = string
      # file_arrival: the watched bucket and the Workflow its Eventarc events start.
      file_bucket   = optional(string)
      wake_workflow = optional(string)
    }))
    alerts = list(object({
      key         = string
      filter      = string
      emails      = list(string)
      channels    = list(string)
      job_failure = bool
    }))
    # The bundle's schemas: BigQuery datasets.
    schemas = optional(map(object({
      project     = string
      dataset_id  = string
      location    = string
      description = string
      labels      = map(string)
    })), {})
    # The bundle's dashboards: each its spec and where it goes in the bucket.
    dashboards = optional(map(object({
      object  = string
      content = string
    })), {})
    # The notebooks the bundle's notebook_tasks run: each one's .ipynb and where it goes.
    notebooks = optional(map(object({
      object  = string
      content = string
    })), {})
  })
}

variable "gcp_project" {
  description = "GCP project the Workflows, schedulers and Cloud Run jobs live in."
  type        = string
}

variable "enabled" {
  description = "false creates nothing, whatever the plan says."
  type        = bool
  default     = true
}

variable "dbt_image" {
  description = "The platform's dbt image (images/dbt) at this platform version; needed when a dbt_task has no job cluster."
  type        = string
  default     = ""
}

variable "notification_channels" {
  description = "Monitoring notification channel ids added to every job's on_failure alert, besides the bundle's own email_notifications / webhook_notifications."
  type        = list(string)
  default     = []
}

variable "log_bucket" {
  description = "projects/<p>/locations/<l>/buckets/<b> to sink every job execution's logs into. Empty: no sink."
  type        = string
  default     = ""
}
