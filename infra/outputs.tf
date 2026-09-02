output "service_uri" {
  description = "The HTTPS base URL Cloud Run assigned the service; /healthz answers here."
  value       = google_cloud_run_v2_service.mcp.uri
}

# What `.mcp.json`'s "url" wants — the MCP endpoint, not the service root.
output "mcp_url" {
  description = "The streamable-HTTP MCP endpoint clients connect to."
  value       = "${google_cloud_run_v2_service.mcp.uri}/mcp"
}

output "service_account_email" {
  description = "Identity the service runs as; the accessor bindings name it."
  value       = google_service_account.mcp.email
}
