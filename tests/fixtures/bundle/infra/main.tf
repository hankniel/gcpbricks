# A project's terraform root: its rendered plan for one target into the module. In the
# project, the module source is the platform repo at the pinned tag:
#   source = "git::https://github.com/hankniel/gcpbricks.git//terraform/modules/bundle?ref=v0.4.0"
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "target" {
  type    = string
  default = "dev"
}

module "bundle" {
  source      = "../../../../terraform/modules/bundle"
  gcp_project = "my-gcp-project"
  plan        = jsondecode(file("${path.module}/../.mlops/${var.target}.json"))
  dbt_image   = "us-central1-docker.pkg.dev/my-gcp-project/mlops/dbt:0.4.0"
  enabled     = false
}
