terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source = "hashicorp/google"
      # Cloud Run's own IAP: iap_enabled.
      version = ">= 7.21"
    }
  }
}
