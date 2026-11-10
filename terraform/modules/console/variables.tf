variable "gcp_project" {
  description = "GCP project the console runs in and reads the bundles' jobs from."
  type        = string
}

variable "region" {
  description = "Region of the service, and of the Workflows, Cloud Run jobs and Vertex AI resources it shows."
  type        = string
}

variable "image" {
  description = "The console image, images/console (e.g. <registry>/console:0.4.0)."
  type        = string
}

variable "service_account" {
  description = "Account the console calls GCP as; see main.tf for the roles it needs."
  type        = string
}

variable "name" {
  description = "Cloud Run service name."
  type        = string
  default     = "mlops-console"
}

variable "iap_members" {
  description = "Who may open the console (e.g. user:a@x.com, group:g@x.com), besides holders of roles/iap.httpsResourceAccessor on the project."
  type        = list(string)
  default     = []
}

variable "env" {
  description = "Extra environment, e.g. MLOPS_FEATURE_CONFIG."
  type        = map(string)
  default     = {}
}

variable "secret_env" {
  description = "Extra environment whose values are secrets (e.g. MLOPS_OAUTH_CLIENT_SECRET): kept out of plans."
  type        = map(string)
  default     = {}
  sensitive   = true
}

variable "min_instances" {
  description = "Instances kept warm: 1 spares the first visit after a quiet spell a cold start (an idle one bills at Cloud Run's idle rate)."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Upper bound on instances, per revision and for the whole service; idle, the service scales down to min_instances."
  type        = number
  default     = 1
}
