output "api_public_ip" {
  value = module.kinora.api_public_ip
}

output "frontend_public_ip" {
  value = module.kinora.frontend_public_ip
}

output "oss_bucket" {
  value = module.kinora.oss_bucket
}

output "database_url" {
  value     = module.kinora.database_url
  sensitive = true
}

output "redis_url" {
  value     = module.kinora.redis_url
  sensitive = true
}

output "jwt_secret" {
  value     = module.kinora.jwt_secret
  sensitive = true
}

output "mcp_auth_token" {
  value     = module.kinora.mcp_auth_token
  sensitive = true
}

output "next_steps" {
  value = module.kinora.next_steps
}
