output "job_workflow_ids" {
  description = "job -> its Workflow id."
  value       = { for k, w in google_workflows_workflow.job : k => w.id }
}

output "scheduler_names" {
  description = "job -> Cloud Scheduler job name."
  value       = { for k, s in google_cloud_scheduler_job.this : k => s.name }
}

output "cloud_run_jobs" {
  description = "Cloud Run job names the bundle's tasks run on."
  value       = [for k, j in google_cloud_run_v2_job.this : j.name]
}
