output "url" {
  description = "The console's address; IAP asks for a Google sign-in."
  value       = google_cloud_run_v2_service.console.uri
}

output "iap_audience" {
  description = "The audience IAP's JWT carries, which the app checks."
  value       = local.audience
}
