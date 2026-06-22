output "vpc_id" {
  description = "VPC id."
  value       = alicloud_vpc.this.id
}

output "vswitch_ids" {
  description = "Per-zone vswitch ids."
  value       = alicloud_vswitch.this[*].id
}

output "oss_bucket" {
  description = "OSS bucket name for Kinora assets."
  value       = alicloud_oss_bucket.assets.bucket
}

output "oss_s3_endpoint" {
  description = "S3-compatible OSS endpoint the app's ObjectStore targets."
  value       = local.oss_s3_endpoint
}

output "rds_connection_string" {
  description = "RDS PostgreSQL internal endpoint host."
  value       = alicloud_db_instance.postgres.connection_string
}

output "rds_port" {
  description = "RDS PostgreSQL port."
  value       = alicloud_db_instance.postgres.port
}

output "redis_connection_domain" {
  description = "Tair/Redis internal endpoint host."
  value       = alicloud_kvstore_instance.redis.connection_domain
}

output "redis_port" {
  description = "Tair/Redis port."
  value       = alicloud_kvstore_instance.redis.port
}

output "api_public_ip" {
  description = "Public IP of the API node (if internet bandwidth > 0)."
  value       = alicloud_instance.api.public_ip
}

output "mcp_public_ip" {
  description = "Public IP of the MCP node."
  value       = alicloud_instance.mcp.public_ip
}

output "render_worker_public_ips" {
  description = "Public IPs of the render-worker nodes."
  value       = alicloud_instance.render_worker[*].public_ip
}

output "database_url" {
  description = "Assembled async SQLAlchemy DSN (sensitive: contains the DB password)."
  value       = local.database_url
  sensitive   = true
}

output "redis_url" {
  description = "Assembled Redis URL (sensitive: contains the AUTH password)."
  value       = local.redis_url
  sensitive   = true
}

output "jwt_secret" {
  description = "JWT signing secret injected as JWT_SECRET (auto-generated when var.jwt_secret was empty). Read with: terraform output -raw jwt_secret"
  value       = local.jwt_secret
  sensitive   = true
}

output "mcp_auth_token" {
  description = "Bearer token the MCP server requires, injected as MCP_AUTH_TOKEN (auto-generated when var.mcp_auth_token was empty). Read with: terraform output -raw mcp_auth_token"
  value       = local.mcp_auth_token
  sensitive   = true
}

output "next_steps" {
  description = "Post-apply checklist."
  value = join("\n", [
    "1. Build + push the backend image to var.container_image (ACR).",
    "2. SSH to the API node and confirm the container is up: curl http://localhost:8000/health",
    "3. Run migrations once: docker exec kinora-api alembic -c alembic.ini upgrade head",
    "   (this also runs CREATE EXTENSION vector on RDS PostgreSQL).",
    "4. Seed the demo book: python backend/scripts/seed_demo.py --via api --api-url http://<api_public_ip>:8000",
    "5. Flip var.kinora_live_video = true only when you intend to spend Wan video-seconds.",
  ])
}
