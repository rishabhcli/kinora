output "alb_dns_name" {
  description = "Public ALB DNS — point your frontend origin / CORS here."
  value       = module.compute.alb_dns_name
}

output "ecs_cluster" {
  value = module.compute.cluster_name
}

output "mcp_service_dns" {
  description = "Internal Cloud Map DNS for the MCP server (never internet-facing)."
  value       = module.compute.mcp_service_dns
}

output "s3_bucket" {
  value = module.storage.bucket
}

output "rds_address" {
  value = module.database.address
}

output "redis_endpoint" {
  value = module.redis.primary_endpoint
}

output "jwt_secret" {
  value     = module.secrets.jwt_secret
  sensitive = true
}

output "mcp_auth_token" {
  value     = module.secrets.mcp_auth_token
  sensitive = true
}
