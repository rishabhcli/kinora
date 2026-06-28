output "api_public_ip" {
  description = "Public IP of the API node (if internet bandwidth > 0)."
  value       = alicloud_instance.api.public_ip
}

output "api_private_ip" {
  description = "Private IP of the API node."
  value       = alicloud_instance.api.private_ip
}

output "frontend_public_ip" {
  description = "Public IP of the frontend node."
  value       = alicloud_instance.frontend.public_ip
}

output "mcp_private_ip" {
  description = "Private IP of the MCP node (never internet-facing)."
  value       = alicloud_instance.mcp.private_ip
}

output "render_worker_private_ips" {
  description = "Private IPs of the render-worker nodes."
  value       = alicloud_instance.render_worker[*].private_ip
}

output "ingest_worker_private_ips" {
  description = "Private IPs of the ingest-worker nodes."
  value       = alicloud_instance.ingest_worker[*].private_ip
}
