# A root deploying the console. In a project, the module source is the platform repo at the
# pinned tag:
#   source = "git::https://github.com/hankniel/gcpbricks.git//terraform/modules/console?ref=v0.4.0"
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.21"
    }
  }
}

module "console" {
  source          = "../../../terraform/modules/console"
  gcp_project     = "my-gcp-project"
  region          = "us-central1"
  image           = "us-central1-docker.pkg.dev/my-gcp-project/mlops/console:0.4.0"
  service_account = "bundle-runner@my-gcp-project.iam.gserviceaccount.com"
  iap_members     = ["user:admin@example.com"]
}
